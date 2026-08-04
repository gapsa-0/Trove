"""What the Overview dashboard shows: totals, date provenance, timeline.

These read the whole archive rather than a filtered view -- see `_VISIBLE`
in `_common` for why that is not the same predicate Browse uses.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ._common import _NOT_HIDDEN, _QUALITY_OK, _VISIBLE, _root_clause, reading


@reading
def summary(conn: sqlite3.Connection, root_id: int | None = None) -> dict[str, Any]:
    """Top-line Overview dashboard numbers: total files/bytes, the type
    breakdown, how many carry GPS or a resolved date, and the archive's
    overall date range."""
    rc, rp = _root_clause(root_id)
    total, size = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM(size),0) FROM files f WHERE {_VISIBLE}{rc}",
        rp,
    ).fetchone()
    types = [
        {"type": r["media_type"], "count": r["c"], "size": r["s"]}
        for r in conn.execute(
            f"""SELECT media_type, COUNT(*) c, COALESCE(SUM(size),0) s
                FROM files f WHERE {_VISIBLE}{rc}
                GROUP BY media_type ORDER BY s DESC""",
            rp,
        )
    ]
    gps = conn.execute(
        f"""SELECT COUNT(*) FROM files f JOIN geo g ON g.file_id=f.id
            WHERE {_VISIBLE}{rc}""",
        rp,
    ).fetchone()[0]
    enriched = conn.execute(
        f"""SELECT COUNT(*) FROM files f JOIN dates d ON d.file_id=f.id
            WHERE {_VISIBLE}{rc}""",
        rp,
    ).fetchone()[0]
    drange = conn.execute(
        f"""SELECT MIN(d.best_datetime), MAX(d.best_datetime)
            FROM files f JOIN dates d ON d.file_id=f.id
            WHERE {_VISIBLE}{rc} AND d.best_datetime IS NOT NULL""",
        rp,
    ).fetchone()
    return {
        "total": total,
        "size": size,
        "types": types,
        "with_gps": gps,
        "enriched": enriched,
        "date_min": drange[0],
        "date_max": drange[1],
    }


@reading
def date_sources(conn: sqlite3.Connection, root_id: int | None = None) -> dict[str, Any]:
    """Breakdown of which source resolved each file's date, for the Overview
    'Dated' drill-down (Takeout JSON vs EXIF vs filename vs mtime vs none)."""
    rc, rp = _root_clause(root_id)
    total = conn.execute(f"SELECT COUNT(*) FROM files f WHERE {_VISIBLE}{rc}", rp).fetchone()[0]
    rows = conn.execute(
        f"""SELECT d.date_source src, COUNT(*) c
            FROM files f JOIN dates d ON d.file_id=f.id
            WHERE {_VISIBLE}{rc} AND d.best_datetime IS NOT NULL
            GROUP BY d.date_source ORDER BY c DESC""",
        rp,
    ).fetchall()
    sources = [{"source": r["src"] or "unknown", "count": r["c"]} for r in rows]
    dated = sum(s["count"] for s in sources)
    return {"total": total, "dated": dated, "undated": total - dated, "sources": sources}


@reading
def timeline(
    conn: sqlite3.Connection,
    root_id: int | None = None,
    bucket: str = "month",
    year: int | str | None = None,
    month: str | None = None,
    person_id: int | None = None,
    person_ids: list[int] | None = None,
    cluster_id: int | None = None,
) -> dict[str, Any]:
    """Frequency of matching, non-hidden media over time.

    bucket is 'month' or 'year'. The remaining arguments mirror Browse filters
    so the chart and grid can answer the same question.
    """
    rc, rp = _root_clause(root_id)
    span = 7 if bucket == "month" else 4  # 'YYYY-MM' vs 'YYYY'
    where = [_NOT_HIDDEN, "d.best_datetime IS NOT NULL"]
    params: list[Any] = []
    if rc:
        # removeprefix, not lstrip: lstrip strips any leading run of the
        # given *characters* (space/A/N/D), not the literal " AND " prefix.
        where.append(rc.removeprefix(" AND "))
        params += rp
    if year:
        where.append("substr(d.best_datetime,1,4) = ?")
        params.append(str(year))
    if month:
        where.append("substr(d.best_datetime,1,7) = ?")
        params.append(month)
    # Filter through memberships rather than joins: a file with more than
    # one face or cluster row must still contribute only one chart count.
    # Each membership predicate is joined with AND, so choosing multiple
    # people means "media containing everyone selected", not either person.
    selected_people = list(dict.fromkeys(person_ids or ([person_id] if person_id else [])))
    for selected_person in selected_people:
        # Mirrors media()'s person filter (see the comment there): union in
        # person_files so manually tagged files count too, without a
        # correlated EXISTS.
        where.append(
            f"f.id IN (SELECT fa.file_id FROM faces fa WHERE fa.person_id=? "
            f"AND {_QUALITY_OK} "
            "UNION SELECT pf.file_id FROM person_files pf WHERE pf.person_id=?)"
        )
        params.append(selected_person)
        params.append(selected_person)
    if cluster_id:
        where.append(
            "f.id IN (SELECT pcm.file_id FROM place_cluster_members pcm WHERE pcm.cluster_id=?)"
        )
        params.append(cluster_id)
    clause = " AND ".join(where)
    rows = conn.execute(
        f"""SELECT substr(d.best_datetime,1,{span}) period,
                   f.media_type mt, COUNT(*) c
            FROM files f JOIN dates d ON d.file_id=f.id
            WHERE {clause}
            GROUP BY period, mt ORDER BY period""",
        params,
    ).fetchall()
    periods: dict[str, dict] = {}
    for r in rows:
        p = periods.setdefault(r["period"], {"period": r["period"], "total": 0})
        p[r["mt"]] = r["c"]
        p["total"] += r["c"]
    return {"bucket": bucket, "series": list(periods.values())}
