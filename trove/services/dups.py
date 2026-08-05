"""Duplicate groups: the Duplicates panel's summary tile and group listing.

Reads `dup_groups`/`dup_members`, built by `dedup/` -- this module never groups
or picks a canonical itself, only reports what dedup already decided.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any

from ._common import _NOT_HIDDEN, _VISIBLE, _root_clause, reading


@reading
def dup_summary(conn: sqlite3.Connection, root_id: int | None = None) -> dict[str, Any]:
    """The Duplicates panel's summary tile: unique/group/duplicate/reclaimable
    -bytes totals, how much is still waiting to be compared, plus the redundant
    copies broken down by match type (identical vs visual) and by media type."""
    rc, rp = _root_clause(root_id)
    # The archive as the rest of the app counts it: every file, with each
    # duplicate group contributing only its canonical. `hidden` is dedup's own
    # column (nothing else writes it), so this is the same population People,
    # Pets and Browse work over -- and it stays honest before dedup has ever
    # run, when nothing is hidden and every file is still its own unique one.
    unique = conn.execute(f"SELECT COUNT(*) FROM files f WHERE {_NOT_HIDDEN}{rc}", rp).fetchone()[0]
    row = conn.execute(
        f"""SELECT COUNT(*) groups,
                   COALESCE(SUM(g.member_count-1),0) dups,
                   COALESCE(SUM(g.redundant_bytes),0) bytes
            FROM dup_groups g
            JOIN files f ON f.id=g.canonical_file_id
            WHERE 1=1{rc}""",
        rp,
    ).fetchone()
    # Breakdown of the redundant copies: "24,102
    # duplicates" says nothing about what they are. Two cuts of the same
    # rows, so both add up to `duplicates`/`reclaimable` exactly:
    #
    # * by match -- byte-identical to the kept copy, or only visually the
    #   same (a re-compressed export). Decided per MEMBER, not by the
    #   group's `method`: a perceptual group routinely also contains exact
    #   copies, and this is the same rule the duplicate tiles label
    #   themselves with, so the panel can never contradict them.
    # * by media type -- a hundred redundant videos and a hundred redundant
    #   thumbnails are not the same news.
    #
    # `f` is the canonical here, same as in the count above, so the shared
    # _root_clause keeps filtering on the root the group belongs to; `d` is
    # the redundant copy being described.
    by_match: dict[str, dict[str, int]] = {}
    by_media: dict[str, dict[str, int]] = {}
    for r in conn.execute(
        f"""SELECT CASE WHEN d.sha256 IS NOT NULL AND d.sha256 = f.sha256
                        THEN 'identical' ELSE 'visual' END AS match_type,
                   d.media_type AS media_type,
                   COUNT(*) AS n, COALESCE(SUM(d.size), 0) AS bytes
            FROM dup_members m
            JOIN dup_groups g ON g.id = m.group_id
            JOIN files f ON f.id = g.canonical_file_id
            JOIN files d ON d.id = m.file_id
            WHERE m.role = 'duplicate'{rc}
            GROUP BY match_type, d.media_type""",
        rp,
    ):
        for bucket, key in (
            (by_match, r["match_type"]),
            (by_media, r["media_type"] or "other"),
        ):
            acc = bucket.setdefault(key, {"count": 0, "bytes": 0})
            acc["count"] += r["n"]
            acc["bytes"] += r["bytes"]

    def _ranked(
        bucket: dict[str, dict[str, int]], order: list[str] | None = None
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = [{"key": k, **v} for k, v in bucket.items()]
        if order:  # fixed order where one exists (identical first)
            items.sort(key=lambda i: order.index(i["key"]) if i["key"] in order else len(order))
        else:
            items.sort(key=lambda i: (-i["count"], i["key"]))
        return items

    return {
        "unique": unique,
        "pending": _pending(conn, root_id),
        "groups": row["groups"],
        "duplicates": row["dups"],
        "reclaimable": row["bytes"],
        "by_match": _ranked(by_match, ["identical", "visual"]),
        "by_media": _ranked(by_media),
    }


def _pending(conn: sqlite3.Connection, root_id: int | None) -> int:
    """Files this root's last successful grouping run has not accounted for.

    The Duplicates screen says "still to compare" the same way People and Pets
    say "still to scan", and the honest source for it is the marker the dedup
    runner writes (`dedup_runs.covered_files`, see db/database.py): every
    present file beyond what that run covered is either newly scanned or not
    hashed yet, and either way no group has looked at it. No marker at all --
    never run, or invalidated because scan/enrich moved something the
    canonical pick reads -- means a full regroup is owed, which is exactly
    what a pending count equal to the whole archive says.
    """
    rc, rp = _root_clause(root_id)
    present = conn.execute(f"SELECT COUNT(*) FROM files f WHERE {_VISIBLE}{rc}", rp).fetchone()[0]
    where, params = ("", []) if root_id is None else (" WHERE root_id=?", [root_id])
    covered = conn.execute(
        f"SELECT COALESCE(SUM(covered_files),0) FROM dedup_runs{where}", params
    ).fetchone()[0]
    # int() around the result is a no-op at runtime -- both operands are counts
    # SQLite always returns as ints -- but sqlite3.Row.__getitem__ is typed Any,
    # which the `-> int` would otherwise silently swallow (see search.py's
    # semantic_pending, which spells out the same thing).
    return int(max(0, present - covered))


@reading
def dup_groups(
    conn: sqlite3.Connection, root_id: int | None = None, limit: int = 60, offset: int = 0
) -> dict[str, Any]:
    """A page of duplicate groups, largest reclaimable-bytes first, each with
    its canonical and duplicate members (name, folder, role, match type)."""
    rc, rp = _root_clause(root_id)
    groups = conn.execute(
        f"""SELECT g.id, g.method, g.member_count, g.size_each, g.redundant_bytes,
                   g.canonical_file_id
            FROM dup_groups g JOIN files f ON f.id=g.canonical_file_id
            WHERE 1=1{rc}
            ORDER BY g.redundant_bytes DESC, g.id
            LIMIT ? OFFSET ?""",
        (*rp, limit, offset),
    ).fetchall()
    out = []
    for g in groups:
        members = conn.execute(
            """SELECT f.id, f.media_type, f.rel_path, m.role,
                      r.path AS root,
                      CASE
                        WHEN m.role='canonical' THEN 'canonical'
                        WHEN f.sha256 = canonical.sha256 THEN 'identical'
                        ELSE 'visual'
                      END AS match_type
               FROM dup_members m JOIN files f ON f.id=m.file_id
               JOIN roots r ON r.id=f.root_id
               JOIN files canonical ON canonical.id=?
               WHERE m.group_id=? ORDER BY (m.role='duplicate'), f.id""",
            (g["canonical_file_id"], g["id"]),
        ).fetchall()
        out.append(
            {
                "id": g["id"],
                "method": g["method"],
                "count": g["member_count"],
                "size_each": g["size_each"],
                "reclaimable": g["redundant_bytes"],
                "canonical_id": g["canonical_file_id"],
                "members": [
                    {
                        "id": m["id"],
                        "type": m["media_type"],
                        "role": m["role"],
                        "match_type": m["match_type"],
                        "name": os.path.basename(m["rel_path"]),
                        "folder": os.path.dirname(m["rel_path"]),
                    }
                    for m in members
                ],
            }
        )
    return {"groups": out, "offset": offset, "count": len(out)}
