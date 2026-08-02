"""Places, read side: the Map panel's clusters, points and members.

The manual attach/create/merge/undo flows a user drives from the map live in
``places_edit.py``; see ``services/people.py`` for why read and change are
split.

Reads `place_clusters`/`place_cluster_members`, built and kept current by
`geo/clusters.py` -- nothing here re-clusters. It only reports what geo/
already computed; the clustering itself belongs to the places pipeline stage,
and correcting it by hand belongs to ``places_edit.py``.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

from ._common import reading
from .types import MediaItem


def place_clusters(db_path: str, root_id: int, min_media: int = 10) -> dict[str, Any]:
    """List of place clusters for a root. A pure read.

    It used to cluster the root on first call if no rows existed yet, which
    made a GET perform a write: it could fail with "database is locked" while
    the pipeline held the writer (GET routes are correctly not wrapped in
    write_with_retry), and any page on any website could trigger it with an
    <img> pointing at the clusters endpoint. The places stage already
    bootstraps a root that has no clusters -- see pipeline/runners/places.py --
    so this is now only the report, and the map is empty until that stage runs.
    """
    return _read_place_clusters(db_path, root_id, min_media)


# A cluster is exempt from the min-media floor when it's named or pinned: a
# user-named place is intentional regardless of size, and create_place()
# inserts a pinned place with 0-1 members that must survive the read right
# after it's created (see config.place_min_media for the full rationale).
# Written against the `pc` alias so it can be dropped into a join (item()) as
# safely as into a single-table read.
_PLACE_EXEMPT = "(NULLIF(TRIM(pc.name), '') IS NOT NULL OR pc.pinned = 1)"


@reading
def _read_place_clusters(
    conn: sqlite3.Connection, root_id: int, min_media: int = 10
) -> dict[str, Any]:
    clusters = conn.execute(
        f"""SELECT pc.id, pc.name, pc.lat, pc.lon, pc.member_count
           FROM place_clusters pc
           WHERE pc.root_id=? AND (pc.member_count >= ? OR {_PLACE_EXEMPT})
           ORDER BY CASE WHEN NULLIF(TRIM(pc.name), '') IS NULL THEN 1 ELSE 0 END,
                    pc.member_count DESC, pc.id""",
        (root_id, min_media),
    ).fetchall()
    # Honest footnote for the frontend: how many below-threshold clusters
    # (and how many member files) were excluded above, so a "places" count
    # never silently disagrees with what a curious user can add up by hand.
    hidden = conn.execute(
        f"""SELECT COUNT(*), COALESCE(SUM(pc.member_count), 0)
           FROM place_clusters pc
           WHERE pc.root_id=? AND pc.member_count < ? AND NOT {_PLACE_EXEMPT}""",
        (root_id, min_media),
    ).fetchone()
    members = conn.execute(
        """SELECT pcm.cluster_id, pcm.file_id
           FROM place_cluster_members pcm
           JOIN place_clusters pc ON pc.id=pcm.cluster_id
           WHERE pc.root_id=? ORDER BY pcm.cluster_id, pcm.file_id""",
        (root_id,),
    ).fetchall()
    thumbs: dict[int, list[int]] = {}
    for m in members:
        ids = thumbs.setdefault(m["cluster_id"], [])
        if len(ids) < 4:
            ids.append(m["file_id"])
    return {
        "clusters": [
            {
                "id": c["id"],
                "name": c["name"],
                "lat": c["lat"],
                "lon": c["lon"],
                "count": c["member_count"],
                "thumb_ids": thumbs.get(c["id"], []),
            }
            for c in clusters
        ],
        "hidden": {"places": hidden[0], "files": hidden[1]},
    }


@reading
def place_points(conn: sqlite3.Connection, root_id: int, min_media: int = 10) -> dict[str, Any]:
    """Every geotagged file as its own point, for the map's un-clustered view.

    The clustered view answers "where do we keep going back to"; this one
    answers "where was each photo actually taken", which a centroid hides --
    a place spanning a whole town looks identical to one spanning a doorway.

    Rows are compact ``[lat, lon, cluster_id, file_id]`` arrays rather than
    objects: an archive can hold tens of thousands of geotagged files, and the
    key names would be most of the payload. ``cluster_id`` is 0 for a file that
    belongs to no *shown* place (no cluster at all, or one below the min-media
    floor), which the client draws in neutral grey -- the raw view is the whole
    truth about what is geotagged, including the strays the places view omits.
    Coordinates are rounded to 5 decimals (~1 m), far finer than a screen pixel
    at any zoom and a third off the payload.
    """
    rows = conn.execute(
        f"""SELECT g.lat, g.lon, f.id,
                   CASE WHEN pc.id IS NOT NULL
                             AND (pc.member_count >= ? OR {_PLACE_EXEMPT})
                        THEN pc.id ELSE 0 END AS cluster_id
            FROM files f
            JOIN geo g ON g.file_id=f.id
            LEFT JOIN place_cluster_members pcm ON pcm.file_id=f.id
            LEFT JOIN place_clusters pc ON pc.id=pcm.cluster_id
            WHERE f.present=1 AND f.root_id=? AND g.lat IS NOT NULL""",
        (min_media, root_id),
    ).fetchall()
    points = [[round(r["lat"], 5), round(r["lon"], 5), r["cluster_id"], r["id"]] for r in rows]
    unplaced = sum(1 for p in points if not p[2])
    return {"points": points, "unplaced": unplaced}


@reading
def place_cluster_members(
    conn: sqlite3.Connection, cluster_id: int, limit: int = 120, offset: int = 0
) -> dict[str, Any] | None:
    """A page of files belonging to one place cluster, newest-dated first.
    Returns None if ``cluster_id`` doesn't exist."""
    c = conn.execute(
        "SELECT id, name, lat, lon, member_count FROM place_clusters WHERE id=?", (cluster_id,)
    ).fetchone()
    if not c:
        return None
    rows = conn.execute(
        """SELECT f.id, f.media_type, f.rel_path, d.best_datetime AS dt,
                  d.date_source AS dsrc,
                  EXISTS(SELECT 1 FROM geo g WHERE g.file_id=f.id) AS has_gps
           FROM place_cluster_members pcm
           JOIN files f ON f.id=pcm.file_id
           LEFT JOIN dates d ON d.file_id=f.id
           WHERE pcm.cluster_id=?
           ORDER BY (d.best_datetime IS NULL), d.best_datetime
           LIMIT ? OFFSET ?""",
        (cluster_id, limit, offset),
    ).fetchall()
    members: list[MediaItem] = [
        {
            "id": r["id"],
            "type": r["media_type"],
            "name": os.path.basename(r["rel_path"]),
            "date": r["dt"],
            "date_source": r["dsrc"],
            "has_gps": bool(r["has_gps"]),
        }
        for r in rows
    ]
    return {
        "id": c["id"],
        "name": c["name"],
        "lat": c["lat"],
        "lon": c["lon"],
        "total": c["member_count"],
        "members": members,
        "offset": offset,
        "count": len(rows),
        "merges": _place_merges_for(conn, cluster_id),
    }


def _place_merges_for(conn: sqlite3.Connection, cluster_id: int) -> list[dict[str, Any]]:
    """Merges this place can undo. Looked up by survivor_id alone, unlike
    _person_merges_for/_pet_merges_for: place_clusters ids are durable (never
    rebuilt wholesale -- see place_merges' schema comment), so there is no
    "id rotted after a recluster" problem a name-anchor would need to paper
    over."""
    rows = conn.execute(
        """SELECT id, dropped_name, file_ids, created_at FROM place_merges
           WHERE survivor_id=? ORDER BY created_at DESC""",
        (cluster_id,),
    ).fetchall()
    out = []
    for row in rows:
        pairs = json.loads(row["file_ids"])
        out.append(
            {
                "id": row["id"],
                "dropped_name": row["dropped_name"],
                "photos_folded_in": len(pairs),
                "created_at": row["created_at"],
            }
        )
    return out
