"""SQLite access layer.

Thin wrapper around sqlite3 so the storage backend stays swappable. Uses WAL
mode and a schema version tracked via ``PRAGMA user_version``.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
_SCHEMA_SQL = Path(__file__).with_name("schema.sql")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create/upgrade the schema. Idempotent."""
    conn.executescript(_SCHEMA_SQL.read_text())
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
