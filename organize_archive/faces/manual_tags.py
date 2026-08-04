"""Manual "this person is in this photo" tags (``person_files``).

These tags are anchored by person NAME rather than id, because clustering
rebuilds `persons` wholesale on every pass -- so they need re-pointing after
each pass.
"""

from __future__ import annotations

import sqlite3


def repair_manual_person_files(conn: sqlite3.Connection) -> None:
    """Re-point manual person_files rows onto whatever person id currently
    carries the stored name, after a re-cluster has rebuilt `persons`.

    Called from inside faces/cluster.py's clustering transaction (see
    _finalize), on an already-open connection -- no commit here, the caller
    commits.

    If no person currently carries a row's stored name, the row is left
    untouched: the name may come back on a later pass (a rename, another
    re-cluster), and deleting the user's statement just because a clustering
    pass momentarily lost the name would be data loss.

    Re-pointing can collide with an existing row for the same (target
    person, file) -- person_files' primary key is (person_id, file_id) --
    so any losing duplicate is dropped rather than allowed to raise.
    """
    rows = conn.execute("SELECT person_id, file_id, person_name FROM person_files").fetchall()
    for row in rows:
        name_row = conn.execute(
            "SELECT name FROM persons WHERE id=?", (row["person_id"],)
        ).fetchone()
        if name_row and name_row["name"] == row["person_name"]:
            continue  # still points at a person carrying the anchored name
        target = conn.execute(
            "SELECT id FROM persons WHERE name=?", (row["person_name"],)
        ).fetchone()
        if not target:
            continue  # name doesn't exist right now; leave the row alone
        if target["id"] == row["person_id"]:
            continue
        conn.execute(
            "DELETE FROM person_files WHERE person_id=? AND file_id=?",
            (target["id"], row["file_id"]),
        )
        conn.execute(
            "UPDATE person_files SET person_id=? WHERE person_id=? AND file_id=?",
            (target["id"], row["person_id"], row["file_id"]),
        )
