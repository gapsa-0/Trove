"""Schema version migrations in db/database.py -- particularly the
SCHEMA_VERSION 11 -> 12 bump that folds the old *unconditional*
"clear stale video embeddings" one-time cleanup into a version-gated
migration inside init_db().

Before this fix, that DELETE ran on every single init_db() call regardless of
schema version -- and init_db() runs at every job start (pipeline/manager.py) -- so it
took the single writer's lock for a full-table scan on every job, whether or
not there was ever a row to clean up. Gating it on `PRAGMA user_version` makes
it run exactly once per database, same as any other migration here.
"""

from trove.db import database as db


def _insert_video_row(conn, file_id=1):
    conn.execute(
        """INSERT INTO files(id, root_id, rel_path, size, mtime, media_type,
                             sha256, first_seen, last_seen)
           VALUES(?, 1, 'clip.mp4', 10, 0, 'video', 'sha', 'now', 'now')""",
        (file_id,),
    )
    conn.execute(
        """INSERT INTO semantic_embeddings(file_id, source_sha256, model,
               dimensions, embedding, status, input_kind, indexed_at)
           VALUES(?, 'sha', 'm', 1, NULL, 'indexed', 'video', '2026-01-01')""",
        (file_id,),
    )


def test_fresh_database_lands_on_schema_version_12():
    conn = db.connect(":memory:")
    db.init_db(conn)
    # SCHEMA_VERSION has since moved past 12 (faces/animal_detections.frame_offset,
    # for video detection), so this only pins "a fresh db lands on the current
    # version", not the literal number in this test's name.
    assert db.SCHEMA_VERSION >= 12
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


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
    # Landed on (at least) version 12, whatever later migrations have since
    # bumped SCHEMA_VERSION to.
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def _files_indexes(conn) -> dict[str, str]:
    return {
        r["name"]: r["sql"] or ""
        for r in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='files'"
        )
    }


def test_fresh_database_has_the_browse_covering_index_and_not_the_one_it_replaces():
    conn = db.connect(":memory:")
    db.init_db(conn)

    indexes = _files_indexes(conn)
    assert "idx_files_present" not in indexes
    assert "present, hidden, root_id, media_type" in indexes["idx_files_browse"]


def test_upgrade_drops_the_index_the_browse_index_covers():
    """idx_files_present's columns are a prefix of idx_files_browse's, so it can
    never be the better plan -- it only costs every file insert and every
    present/hidden flip a second b-tree write. An existing database must lose
    it, not carry both."""
    conn = db.connect(":memory:")
    conn.executescript(db._SCHEMA_SQL.read_text())
    conn.execute("CREATE INDEX idx_files_present ON files(present)")
    conn.commit()

    db.init_db(conn)

    assert "idx_files_present" not in _files_indexes(conn)


def test_browse_queries_are_answered_from_the_covering_index():
    """The point of the index is that "which files does Browse show" never
    touches the files table, which is the archive's largest by far."""
    conn = db.connect(":memory:")
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id, path, added_at) VALUES(1, '/x', 'now')")
    conn.commit()

    plan = " ".join(
        r["detail"]
        for r in conn.execute(
            """EXPLAIN QUERY PLAN
               SELECT DISTINCT media_type FROM files f
               WHERE f.present = 1 AND f.hidden = 0 AND f.root_id = 1"""
        )
    )
    assert "COVERING INDEX idx_files_browse" in plan


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


def _add_file(conn, file_id=1, root_id=1, rel_path="letter.pdf"):
    conn.execute(
        """INSERT INTO files(id, root_id, rel_path, ext, size, mtime, media_type,
                             sha256, first_seen, last_seen)
           VALUES(?, ?, ?, 'pdf', 10, 0, 'document', 'sha', 'now', 'now')""",
        (file_id, root_id, rel_path),
    )


def _add_chunk(conn, file_id=1, chunk_id=1, text="petición de reembolso"):
    conn.execute(
        """INSERT INTO doc_chunks(id, file_id, ordinal, page_first, page_last, chars)
           VALUES(?, ?, 0, 1, 1, ?)""",
        (chunk_id, file_id, len(text)),
    )
    conn.execute("INSERT INTO doc_chunk_fts(rowid, text) VALUES(?, ?)", (chunk_id, text))


def test_a_fresh_database_carries_the_document_text_tables_and_the_index():
    conn = db.connect(":memory:")
    db.init_db(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"doc_text", "doc_chunks"} <= tables
    # The index is created by a migration rather than by schema.sql, so this is
    # the only one of the three whose presence is conditional.
    assert db.text_index_present(conn) is db.fts5_supported()


def test_an_older_database_gains_the_document_text_tables_on_upgrade():
    """The tables arrive through executescript's CREATE ... IF NOT EXISTS and the
    index through _migrate_text_index, so an archive built before either existed
    picks both up the first time it is opened -- no re-scan, no data migration."""
    conn = db.connect(":memory:")
    conn.executescript(db._SCHEMA_SQL.read_text())
    conn.execute("DROP TABLE doc_text")
    conn.execute("DROP TABLE doc_chunks")
    conn.execute("PRAGMA user_version=13")
    conn.commit()

    db.init_db(conn)

    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"doc_text", "doc_chunks"} <= tables
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_an_older_database_gains_the_edit_log_on_upgrade():
    """The history table arrives the same way the document tables do, so an
    archive edited before it existed simply starts recording from the next
    change rather than needing anything rebuilt."""
    conn = db.connect(":memory:")
    conn.executescript(db._SCHEMA_SQL.read_text())
    conn.execute("DROP TABLE edit_log")
    conn.commit()

    db.init_db(conn)

    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "edit_log" in tables


def test_the_text_index_survives_the_job_start_that_reopens_an_archive():
    """init_db runs at every job start, so creating the index must be a no-op
    once it is there -- and must not lose the rows already written into it."""
    conn = db.connect(":memory:")
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id, path, added_at) VALUES(1, '/x', 'now')")
    _add_file(conn)
    _add_chunk(conn)
    conn.commit()

    db.init_db(conn)
    db.init_db(conn)

    assert conn.execute("SELECT COUNT(*) FROM doc_chunk_fts").fetchone()[0] == 1


def test_deleting_a_roots_files_takes_its_text_index_rows_with_them():
    """The one sync obligation the schema cannot express.

    doc_chunks cascades away with the file, but doc_chunk_fts is a virtual table
    with no foreign key to cascade along -- and SQLite would not fire a delete
    trigger on a cascade anyway, since recursive_triggers is off. reconcile_root
    therefore clears the index by hand. Without that, the rows survive their
    content and every later search reads rowids that address nothing.
    """
    conn = db.connect(":memory:")
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id, path, added_at) VALUES(1, '/gone', 'now')")
    _add_file(conn)
    _add_chunk(conn)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM doc_chunk_fts").fetchone()[0] == 1

    # A root for a different folder: this database has no business holding it,
    # so reconcile_root drops its files wholesale.
    db.reconcile_root(conn, 2, "/elsewhere")

    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM doc_chunks").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM doc_chunk_fts").fetchone()[0] == 0


def test_the_index_folds_accents_so_a_spanish_search_finds_a_spanish_word():
    """`remove_diacritics 2` is why "peticion" finds "petición". Version 2 rather
    than 1 because 1 leaves combining marks that stand as their own codepoint,
    which covers most accented Spanish text."""
    conn = db.connect(":memory:")
    db.init_db(conn)
    # Straight into the index: what is under test is the tokenizer, not the
    # chunk row that would normally accompany it.
    conn.execute("INSERT INTO doc_chunk_fts(rowid, text) VALUES(1, 'petición de reembolso')")
    conn.commit()

    for query in ("peticion", "petición", "REEMBOLSO"):
        found = conn.execute(
            "SELECT COUNT(*) FROM doc_chunk_fts WHERE doc_chunk_fts MATCH ?", (query,)
        ).fetchone()[0]
        assert found == 1, query


# -- 17 -> 18: the verdicts recorded while no video frame could be read -------
# For one release ffmpeg refused every frame extraction, and both stages that
# sample a video wrote that down as final: description search as a permanent
# "unsupported" skip, detection as a scan marker holding zero. Neither is ever
# revisited on its own, so the fixed extractor needs those rows cleared.

_FRAMELESS = "unsupported video: could not extract any frames (ffmpeg missing or unreadable video)"


def _archive_with_a_written_off_video(previous_version: int | None = 17):
    conn = db.connect(":memory:")
    conn.executescript(db._SCHEMA_SQL.read_text())
    if previous_version is not None:
        conn.execute(f"PRAGMA user_version={previous_version}")
    conn.execute("INSERT INTO roots(id, path, added_at) VALUES(1, '/x', 'now')")
    conn.execute(
        """INSERT INTO files(id, root_id, rel_path, ext, size, mtime, media_type,
                             sha256, first_seen, last_seen)
           VALUES(1, 1, 'clip.mp4', 'mp4', 10, 0, 'video', 'sha', 'now', 'now')"""
    )
    conn.execute(
        """INSERT INTO semantic_embeddings(file_id, source_sha256, model, dimensions,
               embedding, status, error, indexed_at)
           VALUES(1, 'sha', 'm', 1, NULL, 'skipped', ?, '2026-08-07')""",
        (_FRAMELESS,),
    )
    conn.execute("INSERT INTO face_scan(file_id, n_faces, scanned_at) VALUES(1, 0, 'now')")
    conn.execute(
        """INSERT INTO pet_scan(file_id, n_animals, model_source, scanned_at)
           VALUES(1, 0, 'm', 'now')"""
    )
    conn.commit()
    return conn


def _leftovers(conn) -> tuple[int, int, int]:
    count = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return (
        count("SELECT COUNT(*) FROM semantic_embeddings"),
        count("SELECT COUNT(*) FROM face_scan"),
        count("SELECT COUNT(*) FROM pet_scan"),
    )


def test_upgrading_reopens_a_video_the_frame_extractor_gave_up_on():
    conn = _archive_with_a_written_off_video()

    db.init_db(conn)

    assert _leftovers(conn) == (0, 0, 0), (
        "the video is still carrying the verdicts recorded while nothing could "
        "be read from it, so no stage will ever look at it again"
    )


def test_a_video_skipped_for_its_own_sake_is_left_alone():
    """Only the frameless verdict is undone. A file the indexer refused for
    some other reason still has a real answer recorded, and re-running it
    would reproduce the same one."""
    conn = _archive_with_a_written_off_video()
    conn.execute(
        "UPDATE semantic_embeddings SET error='media exceeds the size limit' WHERE file_id=1"
    )
    conn.commit()

    db.init_db(conn)

    assert conn.execute("SELECT COUNT(*) FROM semantic_embeddings").fetchone()[0] == 1


def test_a_photo_keeps_the_detection_work_already_done_for_it():
    """The detect half of the fix clears by media type, since a scan marker
    cannot say whether looking was possible. It must not reach past videos."""
    conn = _archive_with_a_written_off_video()
    conn.execute(
        """INSERT INTO files(id, root_id, rel_path, ext, size, mtime, media_type,
                             sha256, first_seen, last_seen)
           VALUES(2, 1, 'photo.jpg', 'jpg', 10, 0, 'image', 'sha2', 'now', 'now')"""
    )
    conn.execute("INSERT INTO face_scan(file_id, n_faces, scanned_at) VALUES(2, 3, 'now')")
    conn.commit()

    db.init_db(conn)

    kept = conn.execute("SELECT file_id, n_faces FROM face_scan").fetchall()
    assert [tuple(r) for r in kept] == [(2, 3)]


def test_an_already_current_database_does_not_pay_for_the_cleanup_again():
    """init_db runs at every job start, so a cleanup that already ran must not
    keep taking the writer's lock -- and must not wipe detection work redone
    since."""
    conn = _archive_with_a_written_off_video(previous_version=None)
    db.init_db(conn)
    # OR REPLACE so this stands for "detection has since run again", whether or
    # not the call above cleared the marker -- the assertion below is what says
    # which happened, rather than a constraint violation up here.
    conn.execute(
        "INSERT OR REPLACE INTO face_scan(file_id, n_faces, scanned_at) VALUES(1, 2, 'now')"
    )
    conn.commit()

    db.init_db(conn)

    assert conn.execute("SELECT n_faces FROM face_scan WHERE file_id=1").fetchone()[0] == 2
