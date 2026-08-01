"""Places: the Map panel's clusters, points, and the manual attach/create/
merge/undo flows a user drives from it.

Reads and writes `place_clusters`/`place_cluster_members`, built and kept
current by `geo/clusters.py` -- this module never re-clusters on its own
(beyond the one-time bootstrap `recompute_place_clusters` triggers), it only
reports what geo/ already computed and lets a user correct it by hand.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, cast

from ..db import database as db
from . import merging
from ._common import reading, writing
from .types import MediaItem


def place_clusters(db_path: str, root_id: int, min_media: int = 10) -> dict[str, Any]:
    """List of place clusters for a root, computing them the first time
    they're requested (subsequent calls just read the cached rows)."""
    conn = db.open_readonly(db_path)
    try:
        has_rows = conn.execute(
            "SELECT 1 FROM place_clusters WHERE root_id=? LIMIT 1", (root_id,)
        ).fetchone()
    finally:
        conn.close()
    if not has_rows:
        recompute_place_clusters(db_path, root_id)
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


@writing
def recompute_place_clusters(conn: sqlite3.Connection, root_id: int) -> dict[str, Any]:
    """Rebuild place_clusters/place_cluster_members for a root from scratch.
    Returns the resulting cluster and point counts."""
    from ..geo.clusters import cluster_places

    stats = cluster_places(conn, root_id)
    return {"clusters": stats.clusters, "points": stats.points}


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


@writing
def rename_place_cluster(
    conn: sqlite3.Connection, cluster_id: int | None, name: str
) -> dict[str, Any]:
    """Set a place cluster's display name. Returns ``{"error": ...}`` if
    ``cluster_id`` is missing or doesn't exist."""
    if not cluster_id:
        return {"error": "missing cluster_id"}
    cur = conn.execute("UPDATE place_clusters SET name=? WHERE id=?", (name or None, cluster_id))
    conn.commit()
    if cur.rowcount == 0:
        return {"error": "not found"}
    return {"ok": True, "name": name or None}


# -- "same place?" merges (durable, true undo) ------------------------------
#
# Unlike merge_persons/merge_pets, this needs no separate links table: places
# are durable entities (geo/clusters.py's cluster_places is a one-time
# bootstrap, assign_unplaced never deletes a place), so nothing will ever
# silently rebuild place_clusters out from under a merge. That makes undo a
# true restore of the dropped row rather than "delete a constraint and hope
# the next recluster sorts it out" -- see unmerge_place_clusters.

_PLACE = merging.EntitySpec(
    singular="place",
    plural="places",
    table="place_clusters",
    columns="id,root_id,name,lat,lon,member_count,pinned",
)


def _place_merge_survivor(pa: sqlite3.Row, pb: sqlite3.Row) -> tuple[sqlite3.Row, sqlite3.Row]:
    """Which of two place_clusters rows (each needing at least id, name, lat,
    lon, member_count, pinned) survives a merge, and which is dropped.

    Extracted verbatim from merge_place_clusters so place_merge_preview can
    determine the same survivor -- and therefore the same merged centre --
    without either copy drifting from the other. Chain: named > pinned >
    larger member_count > lower id. See merge_place_clusters' docstring for
    why `pinned` is folded in ahead of member count."""
    if pa["name"] and not pb["name"]:
        return pa, pb
    if pb["name"] and not pa["name"]:
        return pb, pa
    if pa["pinned"] and not pb["pinned"]:
        return pa, pb
    if pb["pinned"] and not pa["pinned"]:
        return pb, pa
    if (pa["member_count"] or 0) != (pb["member_count"] or 0):
        return (pa, pb) if (pa["member_count"] or 0) > (pb["member_count"] or 0) else (pb, pa)
    if pa["id"] < pb["id"]:
        return pa, pb
    return pb, pa


def _merged_centre(keep: sqlite3.Row, drop: sqlite3.Row) -> tuple[float, float]:
    # Coordinates: a pinned survivor's coordinate is a deliberate user
    # pin and never moves. Otherwise, the merged centroid is the
    # member-count weighted mean of the two inputs -- the same
    # incremental-mean rule assign_unplaced uses when it rolls a new
    # point into an existing place (geo/clusters.py:200-207), just
    # applied to two existing centroids instead of one point.
    if keep["pinned"]:
        return keep["lat"], keep["lon"]
    n_keep = keep["member_count"] or 0
    n_drop = drop["member_count"] or 0
    total = n_keep + n_drop
    if not total:
        return keep["lat"], keep["lon"]
    return (
        (keep["lat"] * n_keep + drop["lat"] * n_drop) / total,
        (keep["lon"] * n_keep + drop["lon"] * n_drop) / total,
    )


def _record_place_merge(
    conn: sqlite3.Connection,
    keep: sqlite3.Row,
    drop: sqlite3.Row,
    survivor_name: str | None,
    survivor_lat_before: float,
    survivor_lon_before: float,
    drop_members: list[tuple[int, str]],
    now: str,
) -> None:
    # Recorded so this merge can be undone later (see
    # unmerge_place_clusters): the losing row in full, its members with
    # their original source, and the survivor's centroid BEFORE this
    # merge (so undo can put it back exactly, not just approximately).
    conn.execute(
        """INSERT INTO place_merges(survivor_id, survivor_name, survivor_name_before,
                                     dropped_name, dropped_lat, dropped_lon,
                                     dropped_pinned, survivor_lat, survivor_lon,
                                     file_ids, created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            keep["id"],
            survivor_name,
            keep["name"],
            drop["name"],
            drop["lat"],
            drop["lon"],
            drop["pinned"],
            survivor_lat_before,
            survivor_lon_before,
            json.dumps(drop_members),
            now,
        ),
    )


@writing
def merge_place_clusters(
    conn: sqlite3.Connection, id_a: int | None, id_b: int | None, name: str | None = None
) -> dict[str, Any]:
    """User confirmed two place clusters are the same location. Merge
    immediately: move every member of the losing side into the survivor and
    delete the losing row. Mirrors merge_persons/merge_pets in signature,
    error strings and the explicit-`name` override (see merge_persons'
    docstring) -- but with two place-specific twists:

    - Clusters in different roots are refused outright: people/pets have no
      root column, but a place does, and merging across archives would be
      a silent cross-archive corruption, not a convenience.
    - `pinned` is folded into the survivor-selection chain ahead of member
      count (named > pinned > count > id): a pinned place is a coordinate
      the user deliberately dropped by hand, which outranks an auto-place's
      mere size but still loses to an explicit name either side carries."""
    pa, pb, err = merging.load_sides(conn, _PLACE, id_a, id_b)
    if err:
        return err
    # load_sides ties err's nullness to pa/pb's, but mypy unpacks the union
    # element-wise and loses that coupling; err is None here so both rows
    # are guaranteed present.
    pa = cast(sqlite3.Row, pa)
    pb = cast(sqlite3.Row, pb)
    if pa["root_id"] != pb["root_id"]:
        return {"error": "cannot merge places from different archives"}
    name, err = merging.resolve_name(pa, pb, name)
    if err:
        return err
    # Survivor: named > pinned > larger member_count > lower id, same
    # deterministic chain as merge_persons/merge_pets with `pinned`
    # inserted (see docstring above). Shared with place_merge_preview via
    # _place_merge_survivor so the two never disagree on the centre.
    keep, drop = _place_merge_survivor(pa, pb)
    now = db.now_iso()
    # Captured before anything is mutated: unmerge_place_clusters needs
    # the losing side's exact row (to re-INSERT it) and its member list
    # with each file's original `source`, so a manual attachment comes
    # back manual rather than being silently downgraded to auto.
    drop_members = [
        (r["file_id"], r["source"])
        for r in conn.execute(
            "SELECT file_id, source FROM place_cluster_members WHERE cluster_id=?",
            (drop["id"],),
        ).fetchall()
    ]
    survivor_lat_before, survivor_lon_before = keep["lat"], keep["lon"]
    # `OR REPLACE`, not a plain UPDATE: the PK is (cluster_id, file_id),
    # and nothing forbids a file already being a member of BOTH places
    # (e.g. a manual attachment alongside an auto one) -- a plain UPDATE
    # would hit a PK collision on that row instead of just collapsing it.
    conn.execute(
        "UPDATE OR REPLACE place_cluster_members SET cluster_id=? WHERE cluster_id=?",
        (keep["id"], drop["id"]),
    )
    conn.execute("DELETE FROM place_clusters WHERE id=?", (drop["id"],))
    new_lat, new_lon = _merged_centre(keep, drop)
    # Recompute member_count from place_cluster_members rather than
    # summing the two prior counts: the OR REPLACE above may have
    # collapsed a file that was a member of both, so a plain sum could
    # overcount.
    new_count = conn.execute(
        "SELECT COUNT(*) FROM place_cluster_members WHERE cluster_id=?", (keep["id"],)
    ).fetchone()[0]
    survivor_name = name or keep["name"] or drop["name"]
    conn.execute(
        "UPDATE place_clusters SET name=?, lat=?, lon=?, member_count=? WHERE id=?",
        (survivor_name, new_lat, new_lon, new_count, keep["id"]),
    )
    _record_place_merge(
        conn, keep, drop, survivor_name, survivor_lat_before, survivor_lon_before, drop_members, now
    )
    conn.commit()
    return {"ok": True, "place": {"id": keep["id"], "name": survivor_name, "count": new_count}}


def _restore_dropped_place(
    conn: sqlite3.Connection, m: sqlite3.Row, survivor_root_id: int, now: str
) -> int:
    """Re-INSERT the place merge_place_clusters deleted. Returns its new id."""
    cur = conn.execute(
        """INSERT INTO place_clusters(root_id, name, lat, lon, member_count,
                                      pinned, created_at)
           VALUES(?,?,?,?,0,?,?)""",
        (
            survivor_root_id,
            m["dropped_name"],
            m["dropped_lat"],
            m["dropped_lon"],
            m["dropped_pinned"],
            now,
        ),
    )
    # lastrowid is None only when no INSERT has run on this cursor; the
    # INSERT immediately above guarantees a rowid.
    return cast(int, cur.lastrowid)


def _move_members_back(
    conn: sqlite3.Connection, file_ids: list[tuple[int, str]], survivor_id: int, restored_id: int
) -> int:
    """Move each recorded member off the survivor and back onto the restored
    place, preserving its original `source` so a manual attachment stays
    manual. Returns the restored place's resulting member_count."""
    for file_id, source in file_ids:
        # The merge's UPDATE left this file's membership row pointing at
        # the survivor (cluster_id=survivor["id"]); that row must be
        # removed here, or the file would end up double-counted -- a
        # member of both the restored place AND the survivor -- since an
        # INSERT into place_cluster_members alone has a different PK
        # (restored_id, file_id) and so cannot collide with (and
        # replace) the survivor's (survivor_id, file_id) row.
        conn.execute(
            "DELETE FROM place_cluster_members WHERE cluster_id=? AND file_id=?",
            (survivor_id, file_id),
        )
        # INSERT OR REPLACE, not plain INSERT: if the file rejoined the
        # restored side under some other path since the merge, this
        # undo still wins it back for the restored place, matching what
        # the merge itself did to a doubly-attached file.
        conn.execute(
            "INSERT OR REPLACE INTO place_cluster_members(cluster_id, file_id, source) "
            "VALUES(?,?,?)",
            (restored_id, file_id, source),
        )
    row = conn.execute(
        "SELECT COUNT(*) FROM place_cluster_members WHERE cluster_id=?", (restored_id,)
    ).fetchone()
    return int(row[0])


def _restore_survivor(conn: sqlite3.Connection, survivor: sqlite3.Row, m: sqlite3.Row) -> None:
    # Restore the survivor's pre-merge centroid verbatim (see docstring
    # for why this isn't recomputed) and its member_count from what
    # actually remains after the moves above.
    survivor_count = conn.execute(
        "SELECT COUNT(*) FROM place_cluster_members WHERE cluster_id=?", (survivor["id"],)
    ).fetchone()[0]
    # ...and its name, but only if nothing has renamed it since the merge.
    # Merging two differently-named places takes an explicit `name` that
    # OVERWRITES the survivor's own name, so without this the loser's name
    # comes back on the restored place while the survivor keeps the chosen
    # one -- the pre-merge pair "A"/"B" would undo to "A"/"A", quietly
    # losing a name the user had typed. Conditional (not unconditional)
    # for the same reason unmerge_persons guards its manual_person
    # restore: a rename made deliberately after the merge outranks this
    # bookkeeping and must not be clobbered.
    survivor_name = (
        m["survivor_name_before"] if survivor["name"] == m["survivor_name"] else survivor["name"]
    )
    conn.execute(
        "UPDATE place_clusters SET name=?, lat=?, lon=?, member_count=? WHERE id=?",
        (survivor_name, m["survivor_lat"], m["survivor_lon"], survivor_count, survivor["id"]),
    )


@writing
def unmerge_place_clusters(conn: sqlite3.Connection, merge_id: int | None) -> dict[str, Any]:
    """Undo a drag-merge recorded by merge_place_clusters.

    Because places are durable (see the module-level comment above), this is
    a true restore rather than "delete a constraint and recluster": the
    dropped place is re-INSERTed with its recorded name/lat/lon/pinned flag
    and root_id (taken from the survivor -- both sides were already checked
    to share one at merge time), its members are moved back with their
    recorded `source` so a manual attachment stays manual, and the
    survivor's pre-merge centroid is restored verbatim rather than
    recomputed (recomputing from the remaining members could drift from the
    original if other files have joined the survivor since the merge).

    Returns no `recluster` flag -- unlike unmerge_persons/unmerge_pets there
    is no clusterer to re-run; the restore above is already complete, so
    server.py must not start a job for this route.

    Known limitation, same family as unmerge_persons' "can't split what the
    clusterer forms on its own": assign_unplaced attaches each newly
    geotagged file to whichever existing place is nearest within 300 m. If a
    photo taken at the absorbed location arrives *after* this merge (while
    the two places were combined), and the merged centroid has since drifted
    away from that spot, assign_unplaced may give it a fresh unnamed place of
    its own rather than the one this undo just restored. Members already
    moved back by this undo are unaffected either way.

    Safe to call twice: a stale/missing merge_id returns a clean error rather
    than raising."""
    if not merge_id:
        return {"error": "missing merge_id"}
    m = conn.execute("SELECT * FROM place_merges WHERE id=?", (merge_id,)).fetchone()
    if not m:
        return {"error": "merge not found"}
    survivor = conn.execute(
        "SELECT id, root_id, name, member_count FROM place_clusters WHERE id=?",
        (m["survivor_id"],),
    ).fetchone()
    if not survivor:
        return {"error": "survivor place no longer exists"}
    restored_id = _restore_dropped_place(conn, m, survivor["root_id"], db.now_iso())
    restored_count = _move_members_back(
        conn, json.loads(m["file_ids"]), survivor["id"], restored_id
    )
    conn.execute(
        "UPDATE place_clusters SET member_count=? WHERE id=?", (restored_count, restored_id)
    )
    _restore_survivor(conn, survivor, m)
    conn.execute("DELETE FROM place_merges WHERE id=?", (merge_id,))
    conn.commit()
    return {"ok": True, "place_id": restored_id}


@reading
def place_merge_preview(
    conn: sqlite3.Connection, id_a: int | None, id_b: int | None, warn_km: float
) -> dict[str, Any]:
    """Read-only "how spread out would this merge be?" check, so the GUI can
    warn before a drag-to-merge is confirmed rather than after it's already
    committed. Mirrors merge_place_clusters' validation and survivor/centre
    logic exactly (via _place_merge_survivor) so the number shown in the
    confirmation dialog can never disagree with where the merge actually
    lands the centroid.

    Deliberately max-distance-from-centre rather than the true farthest-pair
    diameter: computing every pairwise distance among up to tens of thousands
    of members (both clusters combined) is O(n^2), while comparing each
    member once to the (already known) centre is a single O(n) pass. Both
    answer the same practical question -- "is this cluster absurdly spread
    out?" -- and the centre-based one scales to the largest place in the
    archive without a second thought."""
    pa, pb, err = merging.load_sides(conn, _PLACE, id_a, id_b)
    if err:
        return err
    # load_sides ties err's nullness to pa/pb's, but mypy unpacks the union
    # element-wise and loses that coupling; err is None here so both rows
    # are guaranteed present.
    pa = cast(sqlite3.Row, pa)
    pb = cast(sqlite3.Row, pb)
    if pa["root_id"] != pb["root_id"]:
        return {"error": "cannot merge places from different archives"}
    keep, drop = _place_merge_survivor(pa, pb)
    centre_lat, centre_lon = _merged_centre(keep, drop)
    from ..geo.clusters import _haversine_m

    # Every geolocated member of BOTH clusters. Manually-attached members
    # (place_cluster_members.source='manual') have no geo row and so are
    # simply absent from this join -- correctly: a photo with no
    # coordinate of its own can't be "far" from the centre, it has
    # nothing to measure from.
    points = conn.execute(
        """SELECT g.lat, g.lon FROM place_cluster_members pcm
           JOIN geo g ON g.file_id = pcm.file_id
           WHERE pcm.cluster_id IN (?, ?) AND g.lat IS NOT NULL""",
        (id_a, id_b),
    ).fetchall()
    span_km = 0.0
    for p in points:
        d_km = _haversine_m(centre_lat, centre_lon, p["lat"], p["lon"]) / 1000.0
        if d_km > span_km:
            span_km = d_km
    return {"ok": True, "span_km": span_km, "threshold_km": warn_km, "warn": span_km >= warn_km}


@writing
def set_place(
    conn: sqlite3.Connection, file_id: int | None, place_id: int | None
) -> dict[str, Any]:
    """Attach a file to a place as a MANUAL member (membership only, no geo is
    written, so provenance stays honest). Replaces any existing membership."""
    if not file_id or not place_id:
        return {"error": "missing file_id or place_id"}
    pc = conn.execute("SELECT id, name FROM place_clusters WHERE id=?", (place_id,)).fetchone()
    if not pc:
        return {"error": "unknown place"}
    _detach_file_from_places(conn, file_id)
    conn.execute(
        "INSERT OR IGNORE INTO place_cluster_members(cluster_id, file_id, source) "
        "VALUES(?,?, 'manual')",
        (place_id, file_id),
    )
    _recount_place(conn, place_id)
    conn.commit()
    return {"ok": True, "place": {"id": pc["id"], "name": pc["name"]}}


@writing
def clear_place(conn: sqlite3.Connection, file_id: int | None) -> dict[str, Any]:
    """Detach a file from whatever place it's manually or automatically a
    member of. Returns ``{"error": "missing file_id"}`` if none is given,
    otherwise ``{"ok": True, "place": None}`` even if it had no place."""
    if not file_id:
        return {"error": "missing file_id"}
    _detach_file_from_places(conn, file_id)
    conn.commit()
    return {"ok": True, "place": None}


@writing
def create_place(
    conn: sqlite3.Connection,
    root_id: int | None,
    name: str,
    lat: Any,  # JSON body value, unvalidated until the float() conversion below
    lon: Any,  # JSON body value, unvalidated until the float() conversion below
    file_id: int | None = None,
) -> dict[str, Any]:
    """Create a user-pinned place (fixed coordinate) and optionally attach a file
    to it in one call. The dropped map pin becomes the place's coordinate."""
    if root_id is None or lat is None or lon is None:
        return {"error": "missing root_id / lat / lon"}
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return {"error": "bad coordinates"}
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return {"error": "coordinates out of range"}
    if not conn.execute("SELECT 1 FROM roots WHERE id=?", (root_id,)).fetchone():
        return {"error": "unknown archive"}
    cur = conn.execute(
        """INSERT INTO place_clusters(root_id, name, lat, lon, member_count,
                                      pinned, created_at)
           VALUES(?,?,?,?,0,1,?)""",
        (root_id, (name or "").strip() or None, lat, lon, db.now_iso()),
    )
    # lastrowid is None only when no INSERT has run on this cursor; the
    # INSERT immediately above guarantees a rowid.
    cid = cast(int, cur.lastrowid)
    if file_id:
        _detach_file_from_places(conn, file_id)
        conn.execute(
            "INSERT OR IGNORE INTO place_cluster_members(cluster_id, file_id, "
            "source) VALUES(?,?, 'manual')",
            (cid, file_id),
        )
        _recount_place(conn, cid)
    conn.commit()
    pc = conn.execute("SELECT id, name FROM place_clusters WHERE id=?", (cid,)).fetchone()
    return {"ok": True, "id": cid, "place": {"id": pc["id"], "name": pc["name"]}}


def _detach_file_from_places(conn: sqlite3.Connection, file_id: int) -> None:
    affected = [
        r["cluster_id"]
        for r in conn.execute(
            "SELECT cluster_id FROM place_cluster_members WHERE file_id=?", (file_id,)
        )
    ]
    conn.execute("DELETE FROM place_cluster_members WHERE file_id=?", (file_id,))
    for cid in affected:
        _recount_place(conn, cid)


def _recount_place(conn: sqlite3.Connection, cluster_id: int) -> None:
    n = conn.execute(
        "SELECT COUNT(*) FROM place_cluster_members WHERE cluster_id=?", (cluster_id,)
    ).fetchone()[0]
    conn.execute("UPDATE place_clusters SET member_count=? WHERE id=?", (n, cluster_id))
