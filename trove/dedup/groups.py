"""Reading the files a group is picked from, and writing the groups back.

Everything here is catalogue work: which files are eligible, which copy of a
group is the one that stays visible, and the rows that record the answer. The
relations that decide *what belongs together* live in ``bands.py`` and
``edges.py``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import cast

# One candidate row packed for pick_canonical: (id, width, height, size,
# has_side, has_date, best_datetime, rel_path).
Packed = tuple[int, int | None, int | None, int | None, int, int, str | None, str]


def pick_canonical(members: list[Packed]) -> int:
    """Return the best representative, using a stable, documented ordering."""
    return min(
        members,
        key=lambda m: (
            -(m[1] or 0) * (m[2] or 0),  # highest pixel count
            -(m[3] or 0),  # then least-compressed/largest file
            not m[4],
            not m[5],  # then richer provenance
            m[6] is None,
            m[6] or "",  # then earliest resolved date
            m[7],
            m[0],  # then stable path/id
        ),
    )[0]


def load_candidates(conn: sqlite3.Connection, root_id: int | None) -> list[sqlite3.Row]:
    """Every present, hashed file in scope, with the fields canonical-picking needs."""
    root_clause = "" if root_id is None else " AND f.root_id=?"
    root_params = () if root_id is None else (root_id,)
    return conn.execute(
        """SELECT f.sha256 sha, f.id, f.size, f.rel_path,
                  (t.file_id IS NOT NULL) has_side, (d.file_id IS NOT NULL) has_date
                  , d.best_datetime, mm.width, mm.height
           FROM files f
           LEFT JOIN takeout_sidecar t ON t.file_id=f.id
           LEFT JOIN dates d ON d.file_id=f.id
           LEFT JOIN media_meta mm ON mm.file_id=f.id
           WHERE f.present=1 AND f.sha256 IS NOT NULL"""
        + root_clause
        + " ORDER BY f.id",
        root_params,
    ).fetchall()


def clear(conn: sqlite3.Connection, root_id: int | None) -> None:
    """Drop the previous grouping for this archive, un-hiding its members.

    Older versions grouped the whole catalog. If such a group touches this
    archive, the whole group is discarded: retaining it would keep files in a
    *different* archive hidden because of a cross-archive match.

    The members are swept explicitly at the end rather than left to
    ``dup_members``' ``ON DELETE CASCADE``. That cascade only fires where
    ``PRAGMA foreign_keys`` is on, which ``db.connect`` does set -- but a
    grouping pass whose correctness turns on a connection-level setting is one
    stray connection away from silently accumulating members of groups that no
    longer exist, and this costs one indexed sweep to not depend on.
    """
    group_clause = (
        ""
        if root_id is None
        else (
            " WHERE id IN (SELECT DISTINCT m.group_id FROM dup_members m "
            "JOIN files f ON f.id=m.file_id WHERE f.root_id=?)"
        )
    )
    group_params = () if root_id is None else (root_id,)
    old_groups = conn.execute("SELECT id FROM dup_groups" + group_clause, group_params).fetchall()
    if old_groups:
        ids = [row[0] for row in old_groups]
        marks = ",".join("?" for _ in ids)
        conn.execute(
            f"UPDATE files SET hidden=0, dup_group_id=NULL WHERE dup_group_id IN ({marks})",
            ids,
        )
        conn.execute(f"DELETE FROM dup_groups WHERE id IN ({marks})", ids)
    conn.execute("DELETE FROM dup_members WHERE group_id NOT IN (SELECT id FROM dup_groups)")
    conn.execute("DELETE FROM dup_members WHERE group_id NOT IN (SELECT id FROM dup_groups)")


@dataclass
class Written:
    """One written group, as the rows and totals its caller still has to apply."""

    members: list[tuple[int, int, str]]  # (group_id, file_id, role)
    canonical: tuple[int, int]  # (group_id, file_id)
    duplicates: list[tuple[int, int]]  # (group_id, file_id)
    redundant_bytes: int
    method: str


def write(
    conn: sqlite3.Connection,
    members: list[sqlite3.Row],
    by_id: dict[int, sqlite3.Row],
    now: str,
) -> Written:
    """Insert one dup_groups row and return the file rows that follow from it."""
    packed: list[Packed] = [
        (
            m["id"],
            m["width"],
            m["height"],
            m["size"],
            m["has_side"],
            m["has_date"],
            m["best_datetime"],
            m["rel_path"],
        )
        for m in members
    ]
    canon = pick_canonical(packed)
    redundant = sum(m["size"] for m in members if m["id"] != canon)
    method = "exact" if len({m["sha"] for m in members}) == 1 else "perceptual"
    cur = conn.execute(
        """INSERT INTO dup_groups(method, canonical_file_id, member_count,
           size_each, redundant_bytes, created_at)
           VALUES(?,?,?,?,?,?)""",
        (method, canon, len(members), by_id[canon]["size"], redundant, now),
    )
    # An INSERT that didn't raise always sets lastrowid; see
    # db.database.get_or_create_root for why typeshed still widens it.
    gid = cast(int, cur.lastrowid)
    return Written(
        members=[(gid, m["id"], "canonical" if m["id"] == canon else "duplicate") for m in members],
        canonical=(gid, canon),
        duplicates=[(gid, m["id"]) for m in members if m["id"] != canon],
        redundant_bytes=redundant,
        method=method,
    )
