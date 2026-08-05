"""Bringing an existing archive database up to the current schema.

``schema.sql`` creates what is missing; this is everything that cannot be
expressed that way -- columns added to tables that already ship, indexes
retired, a virtual table whose statement is not safe to run unconditionally,
and the one data fix gated on the version a database is arriving from.

Every function here is idempotent, because ``init_db`` calls the lot at **every
job start** (see ``pipeline/manager.py``). That is the constraint the file is
written against: a migration that is already applied must cost a cached page
read, not a write transaction queued behind the pipeline's writer. Where a
statement would take the writer's lock regardless, it is preceded by a
``sqlite_master`` lookup that decides whether to issue it at all.

Imported by ``database.py``, never the other way round.
"""

from __future__ import annotations

import sqlite3

# Whether this interpreter's SQLite has FTS5, probed once (see fts5_supported).
_FTS5_SUPPORTED: bool | None = None


def fts5_supported() -> bool:
    """Whether this interpreter's SQLite was built with FTS5.

    Probed once per process against a throwaway in-memory database, because it
    is a property of the library rather than of any connection. Verified present
    on the SQLite this project runs on (3.50.2), so this is not expected to be
    False anywhere -- but searching inside documents is the one feature that
    rests entirely on it, and a feature reporting itself unavailable is a far
    better failure than a migration that throws on every archive, including the
    archives that never asked for it.
    """
    global _FTS5_SUPPORTED
    if _FTS5_SUPPORTED is None:
        probe = sqlite3.connect(":memory:")
        try:
            probe.execute("CREATE VIRTUAL TABLE _probe USING fts5(x)")
            _FTS5_SUPPORTED = True
        except sqlite3.Error:
            _FTS5_SUPPORTED = False
        finally:
            probe.close()
    return _FTS5_SUPPORTED


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _migrate_files_and_runs(conn: sqlite3.Connection) -> None:
    """Duplicate grouping on files, and the two columns that let a *finished*
    scan be recognised as finished: which root it covered, and how many files
    were on disk when it completed (see scan_settled)."""
    _add_column_if_missing(conn, "files", "dup_group_id", "INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_dupgroup ON files(dup_group_id)")
    _add_column_if_missing(conn, "scan_runs", "root_id", "INTEGER")
    _add_column_if_missing(conn, "scan_runs", "files_on_disk", "INTEGER")


def _drop_covered_files_index(conn: sqlite3.Connection) -> None:
    """Retire idx_files_present, now that idx_files_browse leads on the same
    column (see schema.sql).

    An index whose columns are a prefix of another's can never be the better
    plan: every predicate it answers, the wider one answers from the same
    entries. Keeping it would cost every file insert and every present/hidden
    flip a second b-tree write, for a structure the planner has stopped
    choosing, so this is a size and write-throughput win rather than a read one.

    Checked before dropped because ``init_db`` runs at every job start: the
    lookup is one read of an already-cached page, where an unconditional DROP
    would open a write transaction each time and queue behind the pipeline's
    writer.
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_files_present'"
    ).fetchone()
    if exists:
        conn.execute("DROP INDEX idx_files_present")


def _migrate_places(conn: sqlite3.Connection) -> None:
    """Durable places: `source` distinguishes GPS-derived ('auto') members from
    ones the user attached by hand ('manual'), which are never wiped; `pinned`
    marks a user-created place whose coordinate is fixed, never recomputed from
    its members."""
    _add_column_if_missing(conn, "place_cluster_members", "source", "TEXT DEFAULT 'auto'")
    conn.execute("UPDATE place_cluster_members SET source='auto' WHERE source IS NULL")
    _add_column_if_missing(conn, "place_clusters", "pinned", "INTEGER NOT NULL DEFAULT 0")


def _migrate_faces(conn: sqlite3.Connection) -> None:
    """Everything the faces table gained after the first schema.

    `manual_person` pins a face to a person *by name* -- the only identity
    stable across the DELETE/rebuild in faces/cluster.py -- and is re-applied
    after every recluster. `persons.centroid` is the cached L2-normalized mean
    embedding, so "same person?" suggestions need not reload every embedding.
    `not_person` is the user's "that is a doll / an animal / a cartoon" verdict,
    which excludes the face from clustering thereafter.

    Quality metrics are stored with algorithm provenance. Pre-existing rows have
    no feature norm to score, so they are left with a NULL `quality_tier`, which
    every consumer reads as BORDERLINE: still clustered and still visible, never
    used to seed a core. That is the safe reading for a face whose quality is
    simply unknown, and it keeps an un-migrated database working until the
    AdaFace re-extract fills the column.
    """
    _add_column_if_missing(conn, "faces", "manual_person", "TEXT")
    _add_column_if_missing(conn, "persons", "centroid", "BLOB")
    _add_column_if_missing(conn, "faces", "not_person", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "faces", "nonhuman_kind", "TEXT")
    _add_column_if_missing(conn, "faces", "nonhuman_source", "TEXT")
    for column in ("focus_score", "brightness", "extreme_fraction", "clipped_fraction"):
        _add_column_if_missing(conn, "faces", column, "REAL")
    _add_column_if_missing(conn, "faces", "quality_score", "REAL")
    _add_column_if_missing(conn, "faces", "quality_source", "TEXT")
    _add_column_if_missing(conn, "faces", "fiqa_norm", "REAL")
    _add_column_if_missing(conn, "faces", "fiqa_score", "REAL")
    _add_column_if_missing(conn, "faces", "fiqa_source", "TEXT")
    _add_column_if_missing(conn, "faces", "quality_tier", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_faces_tier ON faces(quality_tier)")


def _migrate_scan_counters(conn: sqlite3.Connection) -> None:
    """Per-image rejection counters, which make the quality gate inspectable
    without storing rejected doll/cartoon/blurry candidates as faces."""
    _add_column_if_missing(conn, "face_scan", "n_candidates", "INTEGER NOT NULL DEFAULT 0")
    for reason in ("score", "size", "focus", "exposure", "clipped", "nonhuman"):
        _add_column_if_missing(
            conn, "face_scan", f"rejected_{reason}", "INTEGER NOT NULL DEFAULT 0"
        )
    _add_column_if_missing(conn, "pet_scan", "source_sha256", "TEXT")


def _migrate_nonhuman(conn: sqlite3.Connection) -> None:
    """The suppressed-face review queue: enough of the original detection to
    rebuild the face if the user overrules the veto, plus their verdict."""
    _add_column_if_missing(conn, "nonhuman_detections", "embedding", "BLOB")
    _add_column_if_missing(conn, "nonhuman_detections", "source_sha256", "TEXT")
    for column in (
        "det_score",
        "focus_score",
        "brightness",
        "extreme_fraction",
        "clipped_fraction",
        "quality_score",
    ):
        _add_column_if_missing(conn, "nonhuman_detections", column, "REAL")
    _add_column_if_missing(conn, "nonhuman_detections", "quality_source", "TEXT")
    _add_column_if_missing(
        conn, "nonhuman_detections", "review_status", "TEXT NOT NULL DEFAULT 'pending'"
    )
    _add_column_if_missing(conn, "nonhuman_detections", "restored_face_id", "INTEGER")


def _migrate_video(conn: sqlite3.Connection) -> None:
    """Detection now also runs on videos, via sampled keyframes. A box on a video
    is meaningless without the frame it was found in, so both detection tables
    record the ffmpeg offset to re-extract that frame for cropping."""
    _add_column_if_missing(conn, "faces", "frame_offset", "TEXT")
    _add_column_if_missing(conn, "animal_detections", "frame_offset", "TEXT")
    _add_column_if_missing(conn, "semantic_embeddings", "indexer_version", "TEXT")


def _drop_legacy_video_embeddings(conn: sqlite3.Connection) -> None:
    """One-time cleanup, gated to schema version 12 so it runs exactly once ever
    rather than on every init_db call.

    init_db runs at every job start, and an unconditional DELETE here was a
    full-table write taking the single writer's lock every time, independent of
    whether there was anything to clean up. Earlier runs embedded a video's raw
    bytes wholesale (input_kind='video'), which no embedder this app has used
    could accept beyond a tiny clip, so those rows failed forever. Frame-sampled
    video indexing (services/semantic.py) writes a new input_kind
    ('video_frames'), so this DELETE only ever matches the old rows; they become
    pending again exactly once, the first time a pre-12 database is opened.
    """
    conn.execute("DELETE FROM semantic_embeddings WHERE input_kind='video'")


def text_index_present(conn: sqlite3.Connection) -> bool:
    """Whether this database carries the document-text index.

    False on a build without FTS5, and on any archive opened before the index
    existed and not yet re-opened. Callers that write or clear chunks ask this
    rather than assuming, since the index is the one table in the schema that
    ``executescript`` does not guarantee.
    """
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='doc_chunk_fts'"
        ).fetchone()
        is not None
    )


def _migrate_text_index(conn: sqlite3.Connection) -> None:
    """The full-text index over document chunks, created only where FTS5 exists.

    Deliberately not in ``schema.sql``. ``executescript`` runs that file at every
    job start on every archive, and one unsupported statement fails the whole
    script -- so a SQLite without FTS5 would break archives that never asked to
    read a document. Here, such a build simply leaves the index absent and the
    Documents feature reports itself unavailable, in the same words the setup
    panel already uses for a missing dependency.

    **Contentful, not external-content.** An external-content index has to be
    kept in step with ``doc_chunks`` by delete triggers, and SQLite fires AFTER
    DELETE triggers on a foreign-key cascade only when ``PRAGMA
    recursive_triggers`` is on, which it is not. ``reconcile_root`` deletes files
    wholesale, so the index would be left addressing content rows that no longer
    exist -- which for an external-content table makes ``snippet()`` and
    ``bm25()`` return garbage, rather than merely returning too much. Contentful
    turns that same orphan into a stale rowid the search's join drops in
    silence. With no prior FTS5 anywhere in this codebase, the form that fails
    safely is worth more than the copy of the text it costs.

    Checked before created for the reason ``_drop_covered_files_index`` gives:
    this runs at every job start, and a lookup on an already-cached page beats
    opening a write transaction behind the pipeline's writer.
    """
    if not fts5_supported() or text_index_present(conn):
        return
    # remove_diacritics 2 is what lets a search for "peticion" find "petición"
    # -- and version 2 rather than 1 because 1 leaves diacritics that are their
    # own codepoint in place, which is most of them in Spanish.
    conn.execute(
        "CREATE VIRTUAL TABLE doc_chunk_fts "
        "USING fts5(text, tokenize='unicode61 remove_diacritics 2')"
    )


def run(conn: sqlite3.Connection, previous_version: int) -> None:
    """Apply every migration, in order, to an already-scripted database.

    ``previous_version`` is what the file's ``user_version`` said *before*
    ``schema.sql`` was run over it, which is the only thing that can tell a
    one-time data fix whether it is owed -- the ``CREATE ... IF NOT EXISTS``
    statements never touch that number.
    """
    _migrate_files_and_runs(conn)
    _drop_covered_files_index(conn)
    _migrate_places(conn)
    _migrate_faces(conn)
    _migrate_scan_counters(conn)
    _migrate_nonhuman(conn)
    _migrate_video(conn)
    _migrate_text_index(conn)
    if previous_version < 12:
        _drop_legacy_video_embeddings(conn)
