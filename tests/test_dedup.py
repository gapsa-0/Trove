import sqlite3
import threading

from organize_archive.config import Config
from organize_archive.db import database as db
from organize_archive.dedup import exact
from organize_archive.gui import jobs as jobs_mod


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    root = db.get_or_create_root(conn, "/archive")
    return conn, root


def _file(conn, root, path, sha, size):
    conn.execute(
        """INSERT INTO files(root_id, rel_path, ext, size, mtime, media_type,
                             sha256, first_seen, last_seen)
           VALUES(?, ?, 'jpg', ?, 0, 'image', ?, 'now', 'now')""",
        (root, path, size, sha),
    )


def test_exact_grouping_still_works_without_visual_dependencies():
    conn, root = _db()
    _file(conn, root, "one.jpg", "a" * 64, 10)
    _file(conn, root, "two.jpg", "a" * 64, 10)
    conn.commit()

    stats = exact.run(conn)

    assert (stats.groups, stats.duplicate_files, stats.reclaimable_bytes) == (1, 1, 10)
    assert [r[0] for r in conn.execute("SELECT hidden FROM files ORDER BY id")] == [0, 1]


def test_visual_match_merges_different_encodings_and_keeps_best_image(monkeypatch):
    conn, root = _db()
    _file(conn, root, "large.jpg", "a" * 64, 40)
    _file(conn, root, "small.png", "b" * 64, 10)
    _file(conn, root, "different.jpg", "c" * 64, 20)
    conn.executemany("INSERT INTO media_meta(file_id, width, height) VALUES(?, ?, ?)",
                     [(1, 100, 100), (2, 50, 50)])
    conn.commit()
    monkeypatch.setattr(exact, "_perceptual_hashes",
                        lambda *args, **kwargs: ({1: 0, 2: 0x3F, 3: 2**64 - 1}, 2, 0))

    stats = exact.run(conn, Config())

    assert (stats.groups, stats.duplicate_files, stats.reclaimable_bytes) == (1, 1, 10)
    group = conn.execute("SELECT method, canonical_file_id FROM dup_groups").fetchone()
    assert tuple(group) == ("perceptual", 1)


def test_interrupted_regroup_leaves_previous_grouping_intact(monkeypatch):
    """The clear-old-groups + write-new-groups sequence in exact.run() must be
    one transaction with a single commit. Before this fix, `conn.commit()`
    landed right after clearing the old groups (exact.py:228 historically),
    so a crash/cancellation partway through re-grouping published a window --
    permanently, if the process never got to retry -- where every file was
    unhidden and no group existed at all. Simulating that here: establish a
    real grouping, inject a failure inside the grouping loop of a *second*
    run, and confirm a rollback (what a caller does today by simply closing
    the connection without committing on error) restores the first run's
    grouping byte for byte, rather than leaving a half-cleared archive.
    """
    conn, root = _db()
    _file(conn, root, "one.jpg", "a" * 64, 10)
    _file(conn, root, "two.jpg", "a" * 64, 10)
    conn.commit()

    exact.run(conn)  # establish the baseline ("previous") grouping
    before_hidden = [r[0] for r in conn.execute("SELECT hidden FROM files ORDER BY id")]
    before_dup_group_id = [r[0] for r in conn.execute(
        "SELECT dup_group_id FROM files ORDER BY id")]
    before_groups = conn.execute(
        "SELECT method, canonical_file_id, member_count FROM dup_groups").fetchall()
    before_members = conn.execute(
        "SELECT group_id, file_id, role FROM dup_members ORDER BY file_id").fetchall()
    assert len(before_groups) == 1  # sanity: a real grouping exists to protect

    def _boom(*_args, **_kwargs):
        raise RuntimeError("boom mid-grouping")

    monkeypatch.setattr(exact, "_pick_canonical", _boom)

    raised = False
    try:
        exact.run(conn)
    except RuntimeError:
        raised = True
        # What a real caller effectively does on error: discard whatever this
        # connection has not committed (gui/jobs.py's `_run` closes the
        # connection in a `finally` without an intervening commit on the
        # exception path; closing without committing rolls back).
        conn.rollback()
    assert raised, "expected the injected failure to propagate out of run()"

    after_hidden = [r[0] for r in conn.execute("SELECT hidden FROM files ORDER BY id")]
    after_dup_group_id = [r[0] for r in conn.execute(
        "SELECT dup_group_id FROM files ORDER BY id")]
    after_groups = conn.execute(
        "SELECT method, canonical_file_id, member_count FROM dup_groups").fetchall()
    after_members = conn.execute(
        "SELECT group_id, file_id, role FROM dup_members ORDER BY file_id").fetchall()

    assert after_hidden == before_hidden
    assert after_dup_group_id == before_dup_group_id
    assert after_groups == before_groups
    assert after_members == before_members


def _job_manager(tmp_path, monkeypatch):
    # Everything stays under tmp_path: archive_db_path/archive_cache_dir
    # normally resolve under the user's real ~/.local/share/organize_archive,
    # which must never be touched by a test.
    monkeypatch.setattr(Config, "archive_db_path", lambda self, aid: str(tmp_path / "archive.db"))
    monkeypatch.setattr(Config, "archive_cache_dir", lambda self, aid: str(tmp_path / "cache"))
    return jobs_mod.JobManager(Config())


def test_dedup_needed_survives_a_restart(tmp_path, monkeypatch):
    """dedup_needed() must be catalog-derived, not an in-memory flag: a
    successful rebuild recorded by one JobManager has to read back as
    "not needed" from a brand-new JobManager pointed at the same database --
    simulating an app restart -- instead of defaulting to dirty every time
    the process starts (the old bug: a full rebuild ran on every app start)."""
    db_path = tmp_path / "archive.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id, path, added_at) VALUES(1, '/x', 'now')")
    _file(conn, 1, "one.jpg", "a" * 64, 10)
    _file(conn, 1, "two.jpg", "a" * 64, 10)
    conn.commit()

    jm = _job_manager(tmp_path, monkeypatch)
    try:
        assert jm.dedup_needed(1) is True  # never rebuilt yet

        job = jobs_mod.Job(id=1, kind="dedup", root_id=1, root_path="/x")
        jm._run_dedup(conn, job, threading.Event())

        assert jm.dedup_needed(1) is False
    finally:
        jm.shutdown(timeout=2.0)
    conn.close()

    # Simulate a restart: a fresh JobManager with no memory of the run above,
    # reading only what got persisted to the database.
    jm2 = _job_manager(tmp_path, monkeypatch)
    try:
        assert jm2.dedup_needed(1) is False
    finally:
        jm2.shutdown(timeout=2.0)


def test_dedup_needed_true_again_after_new_files_arrive(tmp_path, monkeypatch):
    """A rebuild that covered N files must be considered stale once the
    catalog's present/hashed population changes (a scan added a file) --
    covering the count/max-id half of dedup_needed()'s derivation."""
    db_path = tmp_path / "archive.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id, path, added_at) VALUES(1, '/x', 'now')")
    _file(conn, 1, "one.jpg", "a" * 64, 10)
    conn.commit()

    jm = _job_manager(tmp_path, monkeypatch)
    try:
        job = jobs_mod.Job(id=1, kind="dedup", root_id=1, root_path="/x")
        jm._run_dedup(conn, job, threading.Event())
        assert jm.dedup_needed(1) is False

        _file(conn, 1, "two.jpg", "b" * 64, 5)
        conn.commit()

        assert jm.dedup_needed(1) is True
    finally:
        jm.shutdown(timeout=2.0)
        conn.close()
