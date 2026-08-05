"""Browse: the media grid, the filters above it, and one item's detail panel.

The one part of the app that hides non-canonical duplicates, so everything
here works in `_NOT_HIDDEN` terms while the dashboard counts every present
file. `set_date` lives here because the panel is where a user overrides a
date, even though what it writes is a resolver fact.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, cast

from ._common import (
    _HAS_LOCATION,
    _NOT_HIDDEN,
    _QUALITY_OK,
    _indexed_exists,
    _root_clause,
    reading,
    writing,
)
from .places import _PLACE_EXEMPT
from .types import MediaItem, MediaPage

# The file's own name, in SQL. `rel_path` is stored with the OS separator, so
# both are stripped: `replace` removes every character that is not a separator,
# leaving a set `rtrim` uses to cut the name off the end, which leaves the
# directory prefix -- and replacing that prefix with nothing leaves the name.
# A path with no separator at all yields an empty prefix, and `replace(x,'','')`
# returns x unchanged, which is the wanted answer for a file at the root.
_BASENAME = (
    "replace(f.rel_path, rtrim(f.rel_path, replace(replace(f.rel_path, '/', ''), '\\', '')), '')"
)
# How many words of a name search are honoured. Each one costs a pass over the
# archive's rows (no index can serve a substring), and a name nobody could type
# in full is not a search anyone is waiting on.
_NAME_TOKEN_LIMIT = 8


def _name_tokens(query: str | None) -> list[str]:
    """The words a filename search has to contain, lowercased and de-duplicated.

    Split on whitespace rather than matched as one string, so "beach 2019" finds
    ``2019_beach_trip.jpg`` -- typed order is not the order a camera or an export
    chose. Everything else in a token is kept verbatim: an extension, a dot, a
    dash and an underscore are all things people actually type when they are
    hunting for a file by name, and `instr` treats them as the literal characters
    they are (unlike LIKE, where `_` would quietly match anything).
    """
    seen = dict.fromkeys(word.lower() for word in (query or "").split())
    return list(seen)[:_NAME_TOKEN_LIMIT]


def _media_where(
    *,
    root_id: int | None,
    mtype: str | None,
    name: str | None,
    year: int | str | None,
    month: str | None,
    person_id: int | None,
    person_ids: list[int] | None,
    cluster_id: int | None,
    indexed: bool | None,
    located: bool | None,
    indexer_version: str,
) -> tuple[str, list[Any], str]:
    """Build the Browse grid's WHERE clause and its bound params, plus the
    ``indexed`` sub-select reused in the SELECT list. ``params`` is built up
    positionally in the same order the clauses below are appended -- callers
    bind it positionally, so that order is load-bearing."""
    where = [_NOT_HIDDEN]
    params: list[Any] = []
    rc, rp = _root_clause(root_id)
    if rc:
        where.append(rc.removeprefix(" AND "))
        params += rp
    if mtype:
        where.append("f.media_type = ?")
        params.append(mtype)
    # Filename matching: the search an archive has when it has no search
    # feature. Every word has to appear somewhere in the name, which is what
    # "narrow it down by typing more" means here as well. `instr` over a
    # computed name cannot use an index, so this is a pass over the archive's
    # rows -- acceptable because it only runs while someone is typing at it.
    for token in _name_tokens(name):
        where.append(f"instr(lower({_BASENAME}), ?) > 0")
        params.append(token)
    if year:
        where.append("substr(d.best_datetime,1,4) = ?")
        params.append(str(year))
    if month:
        where.append("substr(d.best_datetime,1,7) = ?")
        params.append(month)
    # Person / place filter on set membership (IN, not a JOIN) so a photo
    # where the same person or place appears twice still yields one grid
    # tile, and the LEFT JOIN dates ordering below is untouched.
    #
    # Use `f.id IN (SELECT file_id ...)` rather than a correlated EXISTS:
    # the subquery materialises the (small) set of the person's/place's
    # file_ids once up front. A correlated EXISTS instead makes SQLite scan
    # every present file (~150k) and re-run the subquery per row, and for
    # the person case it picked idx_faces_person, pulling *all* of that
    # person's faces on each of the 150k iterations, which pushed the
    # request past 120s and left the grid stuck on a bare "Load more".
    selected_people = list(dict.fromkeys(person_ids or ([person_id] if person_id else [])))
    for selected_person in selected_people:
        # UNION of two `SELECT file_id` subqueries inside the same IN, so
        # manually tagged files (person_files, for media with no detected
        # face) match the filter too, without reshaping this into the
        # correlated-EXISTS shape the comment above warns off.
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
    # file_id is semantic_embeddings' INTEGER PRIMARY KEY, so this EXISTS is
    # a rowid lookup per file rather than a scan.
    indexed_sql = _indexed_exists("f")
    if indexed is not None:
        where.append(indexed_sql if indexed else f"NOT {indexed_sql}")
        params.append(indexer_version)
    if located is not None:
        where.append(_HAS_LOCATION if located else f"NOT {_HAS_LOCATION}")
    clause = " AND ".join(where)
    return clause, params, indexed_sql


def _media_items(rows: list[sqlite3.Row]) -> list[MediaItem]:
    """One grid tile per row of the query below. The name is the file's own,
    cut off the stored relative path the way the grid captions it."""
    return [
        {
            "id": r["id"],
            "type": r["media_type"],
            "name": os.path.basename(r["rel_path"]),
            "date": r["dt"],
            "date_source": r["dsrc"],
            "has_gps": bool(r["has_gps"]),
            "indexed": bool(r["indexed"]),
        }
        for r in rows
    ]


@reading
def media(
    conn: sqlite3.Connection,
    *,
    root_id: int | None = None,
    year: int | str | None = None,
    month: str | None = None,
    mtype: str | None = None,
    name: str | None = None,
    person_id: int | None = None,
    person_ids: list[int] | None = None,
    cluster_id: int | None = None,
    sort: str = "newest",
    limit: int = 120,
    offset: int = 0,
    indexed: bool | None = None,
    located: bool | None = None,
) -> MediaPage:
    """The Browse grid. ``indexed`` filters on description-search coverage: True
    for media that can be found by describing it, False for what cannot (still
    queued, skipped, or failed), None for everything.

    ``located`` does the same for media that carries a location.

    The GUI only ever asks for True on either ("Show only …" boxes) -- unchecked
    means no filter at all. False is kept because the predicate is naturally
    three-valued and because the two halves summing to the unfiltered grid is
    the property worth testing.

    ``name`` narrows to the files whose own name contains every word of it. It
    is a filter rather than a ranking -- there is nothing to score, so the grid
    keeps its date order -- and it is one of the three groups Browse's search
    box fills, on every archive.

    It used to be the *fallback*, run only where there was no description index
    to rank against, so an archive that switched Search by description on lost
    the ability to find a file by its name: the words went to the picture model,
    scored below its relevance floor, and came back with nothing."""
    from . import semantic

    clause, params, indexed_sql = _media_where(
        root_id=root_id,
        mtype=mtype,
        name=name,
        year=year,
        month=month,
        person_id=person_id,
        person_ids=person_ids,
        cluster_id=cluster_id,
        indexed=indexed,
        located=located,
        indexer_version=semantic.INDEXER_VERSION,
    )
    total = conn.execute(
        f"""SELECT COUNT(*)
            FROM files f LEFT JOIN dates d ON d.file_id=f.id
            WHERE {clause}""",
        params,
    ).fetchone()[0]
    rows = conn.execute(
        f"""SELECT f.id, f.media_type, f.rel_path, d.best_datetime AS dt,
                   d.date_source AS dsrc,
                   {_HAS_LOCATION} AS has_gps,
                   {indexed_sql} AS indexed
            FROM files f LEFT JOIN dates d ON d.file_id=f.id
            WHERE {clause}
            ORDER BY (d.best_datetime IS NULL),
                     d.best_datetime {"ASC" if sort == "oldest" else "DESC"}, f.id
            LIMIT ? OFFSET ?""",
        (semantic.INDEXER_VERSION, *params, limit, offset),
    ).fetchall()
    items = _media_items(rows)
    return {
        "items": items,
        "offset": offset,
        "limit": limit,
        "count": len(items),
        "total": total,
    }


@reading
def browse_filters(conn: sqlite3.Connection, root_id: int | None = None) -> dict[str, Any]:
    """Options for the Browse filter bar: which year/months, media types, named
    people and named places actually occur in this archive. Scoped to the
    default browse view (_NOT_HIDDEN), so the choices match what the grid shows.

    Only *named* people/places are offered, an unnamed auto-cluster is not a
    label a user would filter by, and naming is how they become meaningful."""
    rc, rp = _root_clause(root_id)
    periods = [
        r[0]
        for r in conn.execute(
            f"""SELECT DISTINCT substr(d.best_datetime,1,7) p
            FROM files f JOIN dates d ON d.file_id=f.id
            WHERE {_NOT_HIDDEN}{rc} AND d.best_datetime IS NOT NULL
            ORDER BY p DESC""",
            rp,
        )
    ]
    types = [
        r[0]
        for r in conn.execute(
            f"""SELECT DISTINCT media_type FROM files f
            WHERE {_NOT_HIDDEN}{rc} ORDER BY media_type""",
            rp,
        )
    ]
    people = [
        {"id": r["id"], "name": r["name"]}
        for r in conn.execute(
            f"""SELECT p.id, p.name, COUNT(DISTINCT fa.file_id) c
            FROM persons p
            JOIN faces fa ON fa.person_id=p.id
            JOIN files f ON f.id=fa.file_id
            WHERE p.name IS NOT NULL AND {_NOT_HIDDEN} AND {_QUALITY_OK}{rc}
            GROUP BY p.id ORDER BY p.name COLLATE NOCASE""",
            rp,
        )
    ]
    if root_id is None:
        place_rows = conn.execute(
            """SELECT id, name FROM place_clusters
               WHERE name IS NOT NULL ORDER BY name COLLATE NOCASE"""
        )
    else:
        place_rows = conn.execute(
            """SELECT id, name FROM place_clusters
               WHERE name IS NOT NULL AND root_id=?
               ORDER BY name COLLATE NOCASE""",
            (root_id,),
        )
    places = [{"id": r["id"], "name": r["name"]} for r in place_rows]
    # Whether the "Searchable" filter has anything to offer yet. EXISTS, not
    # COUNT: the filter bar only needs to know if the option is live, and on
    # a fully indexed archive a count would walk every file to say "lots".
    from . import semantic

    indexed_any = bool(
        conn.execute(
            f"""SELECT EXISTS(SELECT 1 FROM files f
                          WHERE {_NOT_HIDDEN}{rc} AND {_indexed_exists("f")})""",
            (*rp, semantic.INDEXER_VERSION),
        ).fetchone()[0]
    )
    located_any = bool(
        conn.execute(
            f"""SELECT EXISTS(SELECT 1 FROM files f
                          WHERE {_NOT_HIDDEN}{rc} AND {_HAS_LOCATION})""",
            rp,
        ).fetchone()[0]
    )
    return {
        "periods": periods,
        "types": types,
        "people": people,
        "places": places,
        "indexed_any": indexed_any,
        "located_any": located_any,
    }


@reading
def folders(conn: sqlite3.Connection, root_id: int | None, limit: int = 120) -> dict[str, Any]:
    """Return the archive's source tree as a compact, browseable folder list."""
    rc, rp = _root_clause(root_id)
    rows = conn.execute(f"SELECT rel_path FROM files f WHERE {_NOT_HIDDEN}{rc}", rp).fetchall()
    grouped: dict[str, int] = {}
    for row in rows:
        folder = os.path.dirname(row["rel_path"]) or "/"
        grouped[folder] = grouped.get(folder, 0) + 1
    items = sorted(grouped.items(), key=lambda x: (-x[1], x[0].lower()))[:limit]
    return {"folders": [{"path": p, "count": c} for p, c in items]}


@writing
def set_date(conn: sqlite3.Connection, file_id: int | None, value: str) -> dict[str, Any]:
    """Set a manual, variable-precision date. `value` is a YYYY, YYYY-MM or
    YYYY-MM-DD prefix (stored as-is; the whole app groups/sorts by prefix)."""
    if not file_id:
        return {"error": "missing file_id"}
    v = (value or "").strip()
    parts = v.split("-")
    ok = False
    try:
        if len(parts) == 1 and len(parts[0]) == 4:
            y = int(parts[0])
            ok = 1 <= y <= 9999
        elif len(parts) == 2:
            y, mo = int(parts[0]), int(parts[1])
            ok = 1 <= y <= 9999 and 1 <= mo <= 12 and len(parts[1]) == 2
        elif len(parts) == 3:
            y, mo, da = int(parts[0]), int(parts[1]), int(parts[2])
            ok = (
                1 <= y <= 9999
                and 1 <= mo <= 12
                and 1 <= da <= 31
                and len(parts[1]) == 2
                and len(parts[2]) == 2
            )
    except ValueError:
        ok = False
    if not ok:
        return {"error": "date must be YYYY, YYYY-MM or YYYY-MM-DD"}
    if not conn.execute("SELECT 1 FROM files WHERE id=?", (file_id,)).fetchone():
        return {"error": "unknown file"}
    conn.execute(
        """INSERT OR REPLACE INTO dates(file_id, best_datetime, date_source,
                                        date_confidence)
           VALUES(?,?, 'manual', 1.0)""",
        (file_id, v),
    )
    conn.commit()
    return {"ok": True, "date": v, "date_source": "manual"}


def _item_detections(
    conn: sqlite3.Connection, fid: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Detected people and animals for one file, as (people, animals)."""
    people = [
        {
            "person_id": r["person_id"],
            "name": r["name"],
            "face_id": r["face_id"],
        }
        for r in conn.execute(
            f"""SELECT fa.id AS face_id, fa.person_id, p.name
           FROM faces fa LEFT JOIN persons p ON p.id=fa.person_id
           WHERE fa.file_id=? AND fa.not_person=0 AND {_QUALITY_OK}
           ORDER BY fa.det_score DESC""",
            (fid,),
        )
    ]
    animals = [
        {
            "detection_id": row["detection_id"],
            "species": row["species"],
            "pet_id": row["pet_id"],
            "name": row["name"],
            "score": row["det_score"],
        }
        for row in conn.execute(
            """SELECT a.id detection_id,a.species,a.pet_id,p.name,a.det_score
           FROM animal_detections a LEFT JOIN pets p ON p.id=a.pet_id
           WHERE a.file_id=? AND a.species!='teddy bear'
           ORDER BY a.det_score DESC""",
            (fid,),
        )
    ]
    return people, animals


def _item_place(conn: sqlite3.Connection, fid: int, min_media: int) -> sqlite3.Row | None:
    # Current place membership (a file belongs to at most one place).
    # A below-threshold, unnamed/unpinned cluster is not reported as a
    # "place" here either (see place_min_media) — this file just has no
    # location, matching what place_clusters() shows on the map.
    row = conn.execute(
        f"""SELECT pc.id, pc.name FROM place_cluster_members pcm
           JOIN place_clusters pc ON pc.id=pcm.cluster_id
           WHERE pcm.file_id=? AND (pc.member_count >= ? OR {_PLACE_EXEMPT})
           LIMIT 1""",
        (fid, min_media),
    ).fetchone()
    # sqlite3.Cursor.fetchone() is typed Any, so say what this one returns.
    return cast("sqlite3.Row | None", row)


def _item_pick_lists(
    conn: sqlite3.Connection, root_id: int | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    # Pick-lists for in-panel editing: only *named* places (in this file's root)
    # and *named* persons are offered as targets.
    place_options = [
        {"id": r["id"], "name": r["name"]}
        for r in conn.execute(
            """SELECT id, name FROM place_clusters
           WHERE root_id=? AND name IS NOT NULL
           ORDER BY name COLLATE NOCASE""",
            (root_id,),
        )
    ]
    person_options = [
        {"id": r["id"], "name": r["name"]}
        for r in conn.execute(
            """SELECT id, name FROM persons WHERE name IS NOT NULL AND name != ''
           ORDER BY name COLLATE NOCASE"""
        )
    ]
    pet_options = [
        {"id": r["id"], "name": r["name"]}
        for r in conn.execute(
            """SELECT id, name FROM pets WHERE name IS NOT NULL AND name != ''
           ORDER BY name COLLATE NOCASE"""
        )
    ]
    return place_options, person_options, pet_options


def _item_manual_tags(
    conn: sqlite3.Connection, fid: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    # Manually tagged people/pets on this file (person_files/pet_files) --
    # no face/detection exists for these, so there's no face_id/detection_id.
    # Name resolves from the live persons/pets table; if the id has rotted
    # (a re-cluster ran since and repair hasn't caught up yet) fall back to
    # the name stored on the row itself.
    manual_people = [
        {
            "person_id": r["person_id"],
            "name": r["name"] or r["person_name"],
        }
        for r in conn.execute(
            """SELECT pf.person_id, pf.person_name, p.name
           FROM person_files pf LEFT JOIN persons p ON p.id=pf.person_id
           WHERE pf.file_id=?
           ORDER BY COALESCE(p.name, pf.person_name) COLLATE NOCASE""",
            (fid,),
        )
    ]
    manual_pets = [
        {
            "pet_id": r["pet_id"],
            "name": r["name"] or r["pet_name"],
        }
        for r in conn.execute(
            """SELECT pf.pet_id, pf.pet_name, p.name
           FROM pet_files pf LEFT JOIN pets p ON p.id=pf.pet_id
           WHERE pf.file_id=?
           ORDER BY COALESCE(p.name, pf.pet_name) COLLATE NOCASE""",
            (fid,),
        )
    ]
    return manual_people, manual_pets


@reading
def item(conn: sqlite3.Connection, fid: int, min_media: int = 10) -> dict[str, Any] | None:
    """The detail-panel payload for one file: its dates, GPS, metadata,
    people/animals, place, and the pick lists to edit them. Returns None if
    ``fid`` doesn't exist."""
    f = conn.execute(
        """SELECT f.*, r.path AS root_path FROM files f
           JOIN roots r ON r.id=f.root_id WHERE f.id=?""",
        (fid,),
    ).fetchone()
    if not f:
        return None
    d = conn.execute("SELECT * FROM dates WHERE file_id=?", (fid,)).fetchone()
    g = conn.execute("SELECT * FROM geo WHERE file_id=?", (fid,)).fetchone()
    m = conn.execute("SELECT * FROM media_meta WHERE file_id=?", (fid,)).fetchone()
    t = conn.execute("SELECT * FROM takeout_sidecar WHERE file_id=?", (fid,)).fetchone()
    people, animals = _item_detections(conn, fid)
    place = _item_place(conn, fid, min_media)
    place_options, person_options, pet_options = _item_pick_lists(conn, f["root_id"])
    manual_people, manual_pets = _item_manual_tags(conn, fid)
    return {
        "id": fid,
        "name": os.path.basename(f["rel_path"]),
        "rel_path": f["rel_path"],
        "type": f["media_type"],
        "size": f["size"],
        "root_id": f["root_id"],
        "date": d["best_datetime"] if d else None,
        "date_source": d["date_source"] if d else None,
        "date_confidence": d["date_confidence"] if d else None,
        "gps": (
            {"lat": g["lat"], "lon": g["lon"], "alt": g["alt"], "source": g["geo_source"]}
            if g
            else None
        ),
        "meta": (dict(m) if m else None),
        "description": (t["description"] if t else None),
        "people": people,
        "animals": animals,
        "place": ({"id": place["id"], "name": place["name"]} if place else None),
        "place_options": place_options,
        "person_options": person_options,
        "pet_options": pet_options,
        "manual_people": manual_people,
        "manual_pets": manual_pets,
    }


@reading
def media_source(conn: sqlite3.Connection, fid: int) -> tuple[Path, str, int] | None:
    """(absolute path, content sha256, rotate_deg) for a file id, or None.

    The sha256 is what the thumbnail cache is keyed on, so byte-identical
    duplicates (rife in this cross-takeout archive) share one thumbnail and can
    never disagree. Path is DB-derived, so no request-driven path traversal.
    ``rotate_deg`` is the turn detection found this photo's pixels to be stored
    at, on top of EXIF (0 whenever EXIF alone was right)."""
    r = conn.execute(
        """SELECT r.path AS root, f.rel_path, f.sha256,
                  COALESCE(o.rotate_deg, 0) AS rotate_deg
           FROM files f JOIN roots r ON r.id=f.root_id
           LEFT JOIN orientation o ON o.file_id=f.id
           WHERE f.id=?""",
        (fid,),
    ).fetchone()
    if not r:
        return None
    p = Path(r["root"]) / r["rel_path"]
    if not p.is_file():
        return None
    return p, r["sha256"], r["rotate_deg"]
