"""A scan that finished must stop being queued again.

The scan stage sizes its backlog as "files on disk minus rows for this root".
That is a progress estimate, not a completion test: anything the scanner cannot
read is counted on disk and never becomes a row, so the difference stays
positive however many times the archive is walked — and since the scheduler
starts the next ready stage the moment a job ends, the archive was rescanned
from zero over and over.
"""

from __future__ import annotations

import threading

from trove.config import Config
from trove.db import database as db
from trove.pipeline import manager as jobs_mod
from trove.pipeline import stages as stages_mod


class _Stats:
    seen = new = updated = bytes_hashed = unstable = 0


def _job_manager(tmp_path, monkeypatch):
    # Keep every path under tmp_path; the real ones live in the user's home.
    monkeypatch.setattr(Config, "archive_db_path", lambda self, aid: str(tmp_path / "archive.db"))
    monkeypatch.setattr(Config, "archive_cache_dir", lambda self, aid: str(tmp_path / "cache"))
    return jobs_mod.JobManager(Config())


def _catalog(tmp_path, rows: int):
    conn = db.connect(tmp_path / "archive.db")
    db.init_db(conn)
    db.reconcile_root(conn, 1, "/media")
    for n in range(rows):
        conn.execute(
            """INSERT INTO files(root_id, rel_path, size, mtime, media_type, sha256,
                                 first_seen, last_seen)
               VALUES(1, ?, 1, 0, 'image', ?, 'now', 'now')""",
            (f"{n}.jpg", str(n)),
        )
    conn.commit()
    return conn


def _scan_pending(jm, tmp_path, on_disk):
    def monkey(root_id, root_path, max_age=None, allow_walk=True):
        return on_disk

    jm.disk_count = monkey
    states = stages_mod.stage_states(jm.cfg, jm, 1, "/media")
    return next(s for s in states if s["kind"] == "scan")


def test_unreadable_files_do_not_keep_the_scan_queued(tmp_path, monkeypatch):
    """Two of the files on disk could not be hashed, so they never became rows.
    The completed run still covers the archive as it stands."""
    jm = _job_manager(tmp_path, monkeypatch)
    conn = _catalog(tmp_path, rows=8)

    assert _scan_pending(jm, tmp_path, on_disk=10)["state"] == "queued"

    run = db.scan_run_start(conn, 1, ["/media"])
    db.scan_run_finish(conn, run, _Stats(), files_on_disk=10)
    conn.close()

    assert _scan_pending(jm, tmp_path, on_disk=10)["state"] == "up_to_date"
    jm.shutdown(timeout=1)


def test_a_change_on_disk_queues_another_scan(tmp_path, monkeypatch):
    jm = _job_manager(tmp_path, monkeypatch)
    conn = _catalog(tmp_path, rows=10)
    run = db.scan_run_start(conn, 1, ["/media"])
    db.scan_run_finish(conn, run, _Stats(), files_on_disk=10)
    conn.close()

    added = _scan_pending(jm, tmp_path, on_disk=12)
    assert added["state"] == "queued" and added["pending"] == 2
    # Deletions move the count the other way, where a plain subtraction floors
    # at zero and would leave the removed files marked present forever.
    removed = _scan_pending(jm, tmp_path, on_disk=7)
    assert removed["state"] == "queued" and removed["pending"] == 1
    jm.shutdown(timeout=1)


def test_an_interrupted_scan_does_not_count_as_coverage(tmp_path, monkeypatch):
    jm = _job_manager(tmp_path, monkeypatch)
    conn = _catalog(tmp_path, rows=10)
    db.scan_run_start(conn, 1, ["/media"])  # cancelled: never finished
    conn.close()

    assert _scan_pending(jm, tmp_path, on_disk=10)["state"] == "queued"
    jm.shutdown(timeout=1)


def test_status_polling_never_waits_for_a_disk_walk(tmp_path, monkeypatch):
    """Counting 97k files takes ~20s from a cold cache, far longer than the
    client's poll interval, and a walk on the request thread holds the reply for
    all of it -- so the polls stack until the browser has no connection left for
    any other request and the whole archive looks frozen.

    This used to make an exception of the *first* call, on the grounds that
    there was nothing cached to serve. But an archive that was just opened is
    always that case, so the exception was the common path and the endpoint
    blocked exactly when someone was waiting on it. It now answers None, and
    the scan card says it is still counting (see stages._scan_backlog).
    """
    jm = _job_manager(tmp_path, monkeypatch)
    # Which thread each walk ran on, because that -- not whether one happened at
    # all -- is the property. A background walk is exactly what should be kicked
    # off; what must never happen is one on the thread answering the request.
    walked_on = []
    caller = threading.current_thread()

    def count_files(path):
        walked_on.append(threading.current_thread())
        return 5

    monkeypatch.setattr("trove.scan.walker.count_files", count_files)
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: True)

    # Nothing cached: it answers "not known yet" rather than walking here.
    assert jm.disk_count(1, "/media", allow_walk=False) is None
    # Stale rather than absent: still served from the cache, never re-walked
    # inline -- the distinct value proves where the answer came from.
    jm._disk._cache[1] = (0.0, 42)
    assert jm.disk_count(1, "/media", allow_walk=False) == 42
    assert caller not in walked_on, "the polled path walked on the request thread"

    # The scheduler's path is the one allowed to wait for a real number.
    jm._disk._cache.pop(1, None)
    assert jm.disk_count(1, "/media", allow_walk=True) == 5
    assert caller in walked_on, "the scheduler's path did not get a fresh count"
    jm.shutdown(timeout=1)
