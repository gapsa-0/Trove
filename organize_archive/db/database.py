"""SQLite access layer.

Thin wrapper around sqlite3 so the storage backend stays swappable. Uses WAL
mode and a schema version tracked via ``PRAGMA user_version``.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 11
_SCHEMA_SQL = Path(__file__).with_name("schema.sql")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    # busy_timeout MUST be set before any statement that can take a lock. The GUI
    # runs a near-continuous background pipeline that holds the single writer in
    # bursts, while HTTP handler threads issue small writes (rename a person, set a
    # date, attach a place). If busy_timeout is set *after* `PRAGMA journal_mode=WAL`,
    # that pragma — which itself can need a lock — runs under only Python's short
    # default timeout and fails outright with "database is locked" the moment it
    # overlaps a pipeline write. Setting it first makes every later statement wait
    # (retry) for the writer instead of erroring.
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def open_readonly(db_path: str | Path) -> sqlite3.Connection:
    """Open a read-only connection safe to use while a scan is writing.

    Uses a normal connection (so it can read not-yet-checkpointed WAL data) but
    sets ``query_only`` so it never takes a write lock and never contends with
    the single writer.
    """
    conn = connect(db_path)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _add_column_if_missing(conn, table, column, decl):
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db(conn: sqlite3.Connection) -> None:
    """Create/upgrade the schema. Idempotent."""
    conn.executescript(_SCHEMA_SQL.read_text())
    # Migrations for columns added to existing tables.
    _add_column_if_missing(conn, "files", "dup_group_id", "INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_dupgroup ON files(dup_group_id)")
    # Durable-place / editable-detail support:
    #  - place_cluster_members.source distinguishes GPS-derived ('auto') members from
    #    ones the user attached by hand ('manual'); manual members are never wiped.
    #  - place_clusters.pinned marks a user-created place whose coordinate is a fixed
    #    pin (never recomputed from members).
    #  - faces.manual_person pins a face to a person *by name* (the only identity stable
    #    across the DELETE/rebuild in faces/cluster.py), re-applied after every recluster.
    _add_column_if_missing(conn, "place_cluster_members", "source", "TEXT DEFAULT 'auto'")
    conn.execute("UPDATE place_cluster_members SET source='auto' WHERE source IS NULL")
    _add_column_if_missing(conn, "place_clusters", "pinned", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "faces", "manual_person", "TEXT")
    # persons.centroid: cached L2-normalized mean embedding, used to suggest
    # "same person?" merges without reloading every embedding.
    _add_column_if_missing(conn, "persons", "centroid", "BLOB")
    # faces.not_person: user marked this cluster as not a person (doll/animal/
    # cartoon face that YuNet detected); excluded from clustering thereafter.
    _add_column_if_missing(conn, "faces", "not_person", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "faces", "nonhuman_kind", "TEXT")
    _add_column_if_missing(conn, "faces", "nonhuman_source", "TEXT")
    # Face-quality metrics are stored with algorithm provenance. Scan-level
    # rejection counters make quality-gate behavior inspectable without saving
    # rejected doll/cartoon/blurry candidates as faces.
    _add_column_if_missing(conn, "faces", "focus_score", "REAL")
    _add_column_if_missing(conn, "faces", "brightness", "REAL")
    _add_column_if_missing(conn, "faces", "extreme_fraction", "REAL")
    _add_column_if_missing(conn, "faces", "clipped_fraction", "REAL")
    _add_column_if_missing(conn, "faces", "quality_score", "REAL")
    _add_column_if_missing(conn, "faces", "quality_source", "TEXT")
    _add_column_if_missing(conn, "face_scan", "n_candidates", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "face_scan", "rejected_score", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "face_scan", "rejected_size", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "face_scan", "rejected_focus", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "face_scan", "rejected_exposure", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "face_scan", "rejected_clipped", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "face_scan", "rejected_nonhuman", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "nonhuman_detections", "embedding", "BLOB")
    _add_column_if_missing(conn, "nonhuman_detections", "source_sha256", "TEXT")
    _add_column_if_missing(conn, "nonhuman_detections", "det_score", "REAL")
    _add_column_if_missing(conn, "nonhuman_detections", "focus_score", "REAL")
    _add_column_if_missing(conn, "nonhuman_detections", "brightness", "REAL")
    _add_column_if_missing(conn, "nonhuman_detections", "extreme_fraction", "REAL")
    _add_column_if_missing(conn, "nonhuman_detections", "clipped_fraction", "REAL")
    _add_column_if_missing(conn, "nonhuman_detections", "quality_score", "REAL")
    _add_column_if_missing(conn, "nonhuman_detections", "quality_source", "TEXT")
    _add_column_if_missing(
        conn, "nonhuman_detections", "review_status",
        "TEXT NOT NULL DEFAULT 'pending'")
    _add_column_if_missing(conn, "nonhuman_detections", "restored_face_id", "INTEGER")
    _add_column_if_missing(conn, "pet_scan", "source_sha256", "TEXT")
    _add_column_if_missing(conn, "semantic_embeddings", "indexer_version", "TEXT")
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()


def get_or_create_root(conn: sqlite3.Connection, path: str) -> int:
    row = conn.execute("SELECT id FROM roots WHERE path=?", (path,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO roots(path, added_at) VALUES(?, ?)", (path, now_iso())
    )
    conn.commit()
    return cur.lastrowid


def create_root(conn: sqlite3.Connection, root_id: int, path: str) -> None:
    """Insert the single root of a per-archive database with an explicit id.

    Each isolated archive database has exactly one root, and its id is kept
    equal to the archive's id in the app registry — every place that already
    threads a ``root_id`` through (jobs, queries, URLs) keeps meaning the same
    thing without change; only *which database file* it points at changes.
    """
    conn.execute(
        "INSERT INTO roots(id, path, added_at) VALUES(?, ?, ?)",
        (root_id, path, now_iso()),
    )
    conn.commit()
