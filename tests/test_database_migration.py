"""Schema version migrations in db/database.py -- particularly the
SCHEMA_VERSION 11 -> 12 bump that folds the old *unconditional*
"clear stale video embeddings" one-time cleanup into a version-gated
migration inside init_db().

Before this fix, that DELETE ran on every single init_db() call regardless of
schema version -- and init_db() runs at every job start (gui/jobs.py) -- so it
took the single writer's lock for a full-table scan on every job, whether or
not there was ever a row to clean up. Gating it on `PRAGMA user_version` makes
it run exactly once per database, same as any other migration here.
"""

from organize_archive.db import database as db


def _insert_video_row(conn, file_id=1):
    conn.execute(
        """INSERT INTO files(id, root_id, rel_path, size, mtime, media_type,
                             sha256, first_seen, last_seen)
           VALUES(?, 1, 'clip.mp4', 10, 0, 'video', 'sha', 'now', 'now')""",
        (file_id,))
    conn.execute(
        """INSERT INTO semantic_embeddings(file_id, source_sha256, model,
               dimensions, embedding, status, input_kind, indexed_at)
           VALUES(?, 'sha', 'm', 1, NULL, 'indexed', 'video', '2026-01-01')""",
        (file_id,))


def test_fresh_database_lands_on_schema_version_12():
    conn = db.connect(":memory:")
    db.init_db(conn)
    assert db.SCHEMA_VERSION == 12
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 12


def test_pre_12_database_clears_stale_video_embeddings_on_upgrade():
    """A database last touched by schema < 12 predates frame-sampled video
    indexing, so any input_kind='video' row is the old raw-bytes kind that
    always busted Voyage's context window. The first init_db() call that
    upgrades it past 12 must still clear those rows (letting them become
    pending again), preserving the historical unconditional DELETE's outcome
    for a database that actually needs it.
    """
    conn = db.connect(":memory:")
    conn.executescript(db._SCHEMA_SQL.read_text())
    conn.execute("PRAGMA user_version=11")
    conn.execute("INSERT INTO roots(id, path, added_at) VALUES(1, '/x', 'now')")
    _insert_video_row(conn)
    conn.commit()

    db.init_db(conn)

    remaining = conn.execute(
        "SELECT COUNT(*) FROM semantic_embeddings WHERE input_kind='video'"
    ).fetchone()[0]
    assert remaining == 0
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 12


def test_already_current_database_never_reruns_the_video_cleanup():
    """Once a database is at schema version 12, init_db() must leave
    semantic_embeddings alone -- the whole point of gating the migration is
    that a job-start-time init_db() call stops paying for a full-table write
    every time on a cleanup that already ran once."""
    conn = db.connect(":memory:")
    db.init_db(conn)  # brings a fresh db to version 12 with nothing to clean up
    conn.execute("INSERT INTO roots(id, path, added_at) VALUES(1, '/x', 'now')")
    _insert_video_row(conn)
    conn.commit()

    db.init_db(conn)  # must be a no-op here: already at version 12

    remaining = conn.execute(
        "SELECT COUNT(*) FROM semantic_embeddings WHERE input_kind='video'"
    ).fetchone()[0]
    assert remaining == 1
