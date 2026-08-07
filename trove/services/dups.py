"""Duplicate groups: the Duplicates panel's summary tile and group listing.

Reads `dup_groups`/`dup_members`, built by `dedup/` -- this module never groups
or picks a canonical itself, only reports what dedup already decided.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any

from ._common import _NOT_HIDDEN, _VISIBLE, _root_clause, reading


def _add(bucket: dict[str, dict[str, int]], key: str | None, n: int, size: int) -> None:
    """Fold one row into a breakdown. Accumulated rather than assigned: a NULL
    media_type and a literal 'other' land on the same key, and the second must
    not erase the first."""
    acc = bucket.setdefault(key or "other", {"count": 0, "bytes": 0})
    acc["count"] += n
    acc["bytes"] += size


def _ranked(
    bucket: dict[str, dict[str, int]], order: list[str] | None = None
) -> list[dict[str, Any]]:
    """A breakdown as the screen draws it: biggest first, so the bar and its
    legend read in the same order -- or in a fixed order where one exists, which
    is what keeps a bar's colours from swapping as the counts move."""
    items: list[dict[str, Any]] = [{"key": k, **v} for k, v in bucket.items()]
    if order:
        items.sort(key=lambda i: order.index(i["key"]) if i["key"] in order else len(order))
    else:
        items.sort(key=lambda i: (-i["count"], i["key"]))
    return items


def _copy_breakdowns(
    conn: sqlite3.Connection, rc: str, rp: list[Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The redundant copies, cut two ways: "24,102 duplicates" says nothing
    about what they are. Both cuts are of the same rows, so both add up to
    `duplicates`/`reclaimable` exactly.

    * by match -- byte-identical to the kept copy, or only visually the same (a
      re-compressed export). Decided per MEMBER, not by the group's `method`: a
      perceptual group routinely also contains exact copies, and this is the
      same rule the duplicate tiles label themselves with, so the panel can
      never contradict them.
    * by media type -- a hundred redundant videos and a hundred redundant
      thumbnails are not the same news.

    `f` is the canonical, so the shared `_root_clause` keeps filtering on the
    root the group belongs to; `d` is the redundant copy being described.
    """
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
        _add(by_match, r["match_type"], r["n"], r["bytes"])
        _add(by_media, r["media_type"], r["n"], r["bytes"])
    return _ranked(by_match, ["identical", "visual"]), _ranked(by_media)


@reading
def dup_summary(conn: sqlite3.Connection, root_id: int | None = None) -> dict[str, Any]:
    """The Duplicates panel's summary tile: unique/group/duplicate/reclaimable
    -bytes totals, how much is still waiting to be compared, the redundant
    copies broken down by match type (identical vs visual) and by media type,
    plus the unique files themselves broken down by media type.

    The three breakdowns are not one number cut three ways: `by_match` and
    `by_media` both sum to `duplicates`, `by_unique` sums to `unique`."""
    rc, rp = _root_clause(root_id)
    # The archive as the rest of the app counts it: every file, with each
    # duplicate group contributing only its canonical. `hidden` is dedup's own
    # column (nothing else writes it), so this is the same population People,
    # Pets and Browse work over -- and it stays honest before dedup has ever
    # run, when nothing is hidden and every file is still its own unique one.
    unique = conn.execute(f"SELECT COUNT(*) FROM files f WHERE {_NOT_HIDDEN}{rc}", rp).fetchone()[0]
    # The same population cut by media type. Read against `unique`, NOT against
    # `duplicates` like the two cuts in _copy_breakdowns -- a group's copies are
    # one unique file here, so these are the files that survive deduplication,
    # not the ones it hid. The screen labels the row accordingly.
    by_unique: dict[str, dict[str, int]] = {}
    for r in conn.execute(
        f"""SELECT f.media_type AS media_type, COUNT(*) AS n, COALESCE(SUM(f.size), 0) AS bytes
            FROM files f WHERE {_NOT_HIDDEN}{rc}
            GROUP BY f.media_type""",
        rp,
    ):
        _add(by_unique, r["media_type"], r["n"], r["bytes"])
    row = conn.execute(
        f"""SELECT COUNT(*) groups,
                   COALESCE(SUM(g.member_count-1),0) dups,
                   COALESCE(SUM(g.redundant_bytes),0) bytes
            FROM dup_groups g
            JOIN files f ON f.id=g.canonical_file_id
            WHERE 1=1{rc}""",
        rp,
    ).fetchone()
    by_match, by_media = _copy_breakdowns(conn, rc, rp)
    return {
        "unique": unique,
        "pending": _pending(conn, root_id),
        "groups": row["groups"],
        "duplicates": row["dups"],
        "reclaimable": row["bytes"],
        "by_match": by_match,
        "by_media": by_media,
        "by_unique": _ranked(by_unique),
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


# How the listing can be ordered. Keyed by what the screen's control sends,
# valued as SQL fragments rather than built from the parameter, so no caller can
# reach the ORDER BY clause. Every ordering ends on `g.id` so paging is stable:
# member counts tie constantly, and a tie broken differently between two page
# requests drops or repeats groups at the seam.
_SORTS = {
    "": "g.redundant_bytes DESC, g.id",
    "count_desc": "g.member_count DESC, g.id",
    "count_asc": "g.member_count ASC, g.id",
}

# Groups holding at least one copy of a given kind. Decided per MEMBER from its
# sha256, the same rule the tiles and the breakdown panel use -- a perceptual
# group routinely also holds byte-identical copies, so a filter reading the
# group's own `method` would disagree with the tags on the tiles it returned.
_MATCH_HAVING = {
    "identical": "d.sha256 IS NOT NULL AND d.sha256 = f.sha256",
    "visual": "d.sha256 IS NULL OR d.sha256 <> f.sha256",
}


def _match_clause(match: str | None) -> str:
    """An EXISTS restricting the listing to groups holding such a copy."""
    test = _MATCH_HAVING.get(match or "")
    if test is None:
        return ""
    return (
        " AND EXISTS (SELECT 1 FROM dup_members m JOIN files d ON d.id=m.file_id"
        f"            WHERE m.group_id=g.id AND m.role='duplicate' AND ({test}))"
    )


@reading
def dup_groups(
    conn: sqlite3.Connection,
    root_id: int | None = None,
    limit: int = 60,
    offset: int = 0,
    match: str | None = None,
    sort: str | None = None,
) -> dict[str, Any]:
    """A page of duplicate groups, each with its canonical and duplicate members
    (name, folder, role, match type).

    No size comes back with a group, deliberately. ``dup_groups.size_each`` is
    the CANONICAL's size (see dedup/exact.py's ``_write_group``) and is only
    "each" for exact groups, where every copy is byte-identical by definition;
    a perceptual group is routinely a big original beside two re-compressed
    exports, and reporting the original's size as what all three weigh
    contradicted ``redundant_bytes`` beside it, which sums what the copies
    actually take up. ``redundant_bytes`` is the honest figure and the only one
    the listing states -- the size a copy itself weighs belongs on the copy,
    where the viewer already shows it.

    ``match`` keeps only groups holding at least one identical copy, or at least
    one visual match; ``sort`` orders by member count either way. Both fall back
    to the unfiltered, biggest-saving-first listing on any value the screen does
    not offer, so a hand-edited URL narrows nothing and reveals nothing.

    ``total`` counts the groups the filter matches, not the page -- the screen's
    tiles describe the whole archive, and a filtered list needs to be able to say
    how much of it is being shown without contradicting them.
    """
    rc, rp = _root_clause(root_id)
    where = f"WHERE 1=1{rc}{_match_clause(match)}"
    order = _SORTS.get(sort or "", _SORTS[""])
    total = conn.execute(
        f"SELECT COUNT(*) FROM dup_groups g JOIN files f ON f.id=g.canonical_file_id {where}",
        rp,
    ).fetchone()[0]
    groups = conn.execute(
        f"""SELECT g.id, g.method, g.member_count, g.size_each, g.redundant_bytes,
                   g.canonical_file_id
            FROM dup_groups g JOIN files f ON f.id=g.canonical_file_id
            {where}
            ORDER BY {order}
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
    return {"groups": out, "offset": offset, "count": len(out), "total": total}
