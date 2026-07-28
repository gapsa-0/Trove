"""Read-only queries backing the GUI. Each opens its own connection so the
handler stays thread-safe under ThreadingHTTPServer.

Everything is scoped to an archive (a row in `roots`, one per isolated
database, see ``archives()`` below). A root_id of None on the other query
functions means "no filter" (harmless: each archive's database only ever
has the one root anyway).
"""

from __future__ import annotations

import os
import json
import math
import struct
import shutil
from collections import Counter
from pathlib import Path

from ..db import database as db
from ..paths import archive_dir as archive_dir_path

# Analytics (summary, timeline, map, counts) describe the whole archive, so they
# count every present file. Only Browse hides non-canonical duplicates.
_VISIBLE = "f.present = 1"
_NOT_HIDDEN = "f.present = 1 AND f.hidden = 0"


def _quality_ok(alias: str = "fa") -> str:
    """SQL predicate excluding faces the FIQA gate rejected (faces/fiqa.py).

    LOW_QUALITY faces are kept in the database — never deleted, so the call stays
    auditable and re-tierable — but they must not appear anywhere in the UI: not
    in a photo's detected faces, not in a person's photo list, not in any count.
    This predicate is that rule, in one place.

    NULL-safe on purpose: a face extracted before the gate existed has no tier,
    and an unknown quality must not make a face vanish. It reads as BORDERLINE,
    which is exactly how faces/cluster.py treats it too.
    """
    return f"COALESCE({alias}.quality_tier, 'BORDERLINE') != 'LOW_QUALITY'"


_QUALITY_OK = _quality_ok()


def _root_clause(root_id):
    if root_id is None:
        return "", []
    return " AND f.root_id = ?", [root_id]


# -- archives ---------------------------------------------------------------
#
# Each archive is fully self-contained: its own database (cfg.archive_db_path),
# its own thumbnail/face-crop cache (cfg.archive_cache_dir). The registry of
# which archives exist lives in Config (config.json), not in any one database,
# since there is no longer a single shared database to hold a `roots` table
# for all of them.

def archives(cfg) -> list[dict]:
    out = []
    for entry in sorted(cfg.archives, key=lambda a: a["id"]):
        rid, path = entry["id"], entry["path"]
        db_path = cfg.archive_db_path(rid)
        row = {
            "id": rid, "path": path,
            "name": os.path.basename(path.rstrip("/")) or path,
            "added_at": entry["added_at"],
            "files": 0, "size": 0, "hashed": 0,
            "exists": Path(path).is_dir(),
            "last_scan": None, "covers": [],
        }
        if Path(db_path).is_file():
            conn = db.open_readonly(db_path)
            try:
                stats = conn.execute(
                    f"""SELECT COUNT(*) c, COALESCE(SUM(size),0) s,
                               SUM(sha256 IS NOT NULL) hashed
                        FROM files f WHERE {_VISIBLE}"""
                ).fetchone()
                last = conn.execute(
                    "SELECT started_at, finished_at FROM scan_runs ORDER BY id DESC LIMIT 1"
                ).fetchone()
                # A few canonical image ids for the start-page cover mosaic. Served
                # by the root-scoped /archivethumb route (no archive is "open" on
                # the picker). Newest first so the cover reflects recent additions.
                covers = [r[0] for r in conn.execute(
                    f"""SELECT f.id FROM files f
                        WHERE {_NOT_HIDDEN} AND f.media_type='image'
                        ORDER BY f.id DESC LIMIT 5""").fetchall()]
                row.update(
                    files=stats["c"], size=stats["s"], hashed=stats["hashed"] or 0,
                    covers=covers,
                    last_scan=(last["finished_at"] or last["started_at"]) if last else None,
                )
            finally:
                conn.close()
        out.append(row)
    return out


def add_archive(cfg, path: str) -> dict:
    p = Path(path).expanduser()
    if not p.is_dir():
        return {"error": f"Not a directory: {path}"}
    resolved = str(p.resolve())
    if any(a["path"] == resolved for a in cfg.archives):
        return {"error": "That folder is already an archive."}
    # Build the private store first and register it only once it is usable, so a
    # failure here cannot leave a half-added archive in config.json that appears
    # out of nowhere on the next page load.
    aid = cfg.allocate_archive_id()
    try:
        conn = db.connect(cfg.archive_db_path(aid))
        try:
            db.init_db(conn)
            db.reconcile_root(conn, aid, resolved)
        finally:
            conn.close()
    except Exception as exc:
        shutil.rmtree(archive_dir_path(aid), ignore_errors=True)
        return {"error": f"Could not prepare a catalog for that folder: {exc}"}
    entry = cfg.register_archive(aid, resolved)
    return {"id": entry["id"], "path": resolved}


def remove_archive(cfg, root_id: int) -> dict:
    """Forget one archive: delete its private database and cache wholesale.

    Nothing is shared between archives, so unlike the old shared-catalog
    design this never needs to touch any other archive's rows or cache files.
    """
    if not isinstance(root_id, int):
        return {"error": "root_id is required"}
    path = cfg.archive_path(root_id)
    if path is None:
        return {"error": "archive not found"}
    shutil.rmtree(archive_dir_path(root_id), ignore_errors=True)
    cfg.remove_archive_entry(root_id)
    return {"ok": True, "path": path}


# -- dashboard --------------------------------------------------------------

def summary(db_path: str, root_id=None) -> dict:
    conn = db.open_readonly(db_path)
    try:
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
                    GROUP BY media_type ORDER BY s DESC""", rp)
        ]
        gps = conn.execute(
            f"""SELECT COUNT(*) FROM files f JOIN geo g ON g.file_id=f.id
                WHERE {_VISIBLE}{rc}""", rp).fetchone()[0]
        enriched = conn.execute(
            f"""SELECT COUNT(*) FROM files f JOIN dates d ON d.file_id=f.id
                WHERE {_VISIBLE}{rc}""", rp).fetchone()[0]
        drange = conn.execute(
            f"""SELECT MIN(d.best_datetime), MAX(d.best_datetime)
                FROM files f JOIN dates d ON d.file_id=f.id
                WHERE {_VISIBLE}{rc} AND d.best_datetime IS NOT NULL""", rp).fetchone()
        return {
            "total": total, "size": size, "types": types, "with_gps": gps,
            "enriched": enriched,
            "date_min": drange[0], "date_max": drange[1],
        }
    finally:
        conn.close()


def date_sources(db_path: str, root_id=None) -> dict:
    """Breakdown of which source resolved each file's date, for the Overview
    'Dated' drill-down (Takeout JSON vs EXIF vs filename vs mtime vs none)."""
    conn = db.open_readonly(db_path)
    try:
        rc, rp = _root_clause(root_id)
        total = conn.execute(
            f"SELECT COUNT(*) FROM files f WHERE {_VISIBLE}{rc}", rp).fetchone()[0]
        rows = conn.execute(
            f"""SELECT d.date_source src, COUNT(*) c
                FROM files f JOIN dates d ON d.file_id=f.id
                WHERE {_VISIBLE}{rc} AND d.best_datetime IS NOT NULL
                GROUP BY d.date_source ORDER BY c DESC""", rp).fetchall()
        sources = [{"source": r["src"] or "unknown", "count": r["c"]} for r in rows]
        dated = sum(s["count"] for s in sources)
        return {"total": total, "dated": dated, "undated": total - dated,
                "sources": sources}
    finally:
        conn.close()


def timeline(db_path: str, root_id=None, bucket="month", year=None, month=None,
             person_id=None, person_ids=None, cluster_id=None) -> dict:
    """Frequency of matching, non-hidden media over time.

    bucket is 'month' or 'year'. The remaining arguments mirror Browse filters
    so the chart and grid can answer the same question.
    """
    conn = db.open_readonly(db_path)
    try:
        rc, rp = _root_clause(root_id)
        span = 7 if bucket == "month" else 4      # 'YYYY-MM' vs 'YYYY'
        where = [_NOT_HIDDEN, "d.best_datetime IS NOT NULL"]
        params: list = []
        if rc:
            where.append(rc.lstrip(" AND "))
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
        selected_people = list(dict.fromkeys(
            person_ids or ([person_id] if person_id else [])))
        for selected_person in selected_people:
            # Mirrors media()'s person filter (see the comment there): union in
            # person_files so manually tagged files count too, without a
            # correlated EXISTS.
            where.append(
                f"f.id IN (SELECT fa.file_id FROM faces fa WHERE fa.person_id=? "
                f"AND {_QUALITY_OK} "
                "UNION SELECT pf.file_id FROM person_files pf WHERE pf.person_id=?)")
            params.append(selected_person)
            params.append(selected_person)
        if cluster_id:
            where.append(
                "f.id IN (SELECT pcm.file_id FROM place_cluster_members pcm "
                "WHERE pcm.cluster_id=?)")
            params.append(cluster_id)
        clause = " AND ".join(where)
        rows = conn.execute(
            f"""SELECT substr(d.best_datetime,1,{span}) period,
                       f.media_type mt, COUNT(*) c
                FROM files f JOIN dates d ON d.file_id=f.id
                WHERE {clause}
                GROUP BY period, mt ORDER BY period""", params).fetchall()
        periods: dict[str, dict] = {}
        for r in rows:
            p = periods.setdefault(r["period"], {"period": r["period"], "total": 0})
            p[r["mt"]] = r["c"]
            p["total"] += r["c"]
        return {"bucket": bucket, "series": list(periods.values())}
    finally:
        conn.close()


def place_clusters(db_path: str, root_id: int, min_media: int = 10) -> dict:
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


def _read_place_clusters(db_path: str, root_id: int, min_media: int = 10) -> dict:
    conn = db.open_readonly(db_path)
    try:
        clusters = conn.execute(
            f"""SELECT pc.id, pc.name, pc.lat, pc.lon, pc.member_count
               FROM place_clusters pc
               WHERE pc.root_id=? AND (pc.member_count >= ? OR {_PLACE_EXEMPT})
               ORDER BY CASE WHEN NULLIF(TRIM(pc.name), '') IS NULL THEN 1 ELSE 0 END,
                        pc.member_count DESC, pc.id""",
            (root_id, min_media)).fetchall()
        # Honest footnote for the frontend: how many below-threshold clusters
        # (and how many member files) were excluded above, so a "places" count
        # never silently disagrees with what a curious user can add up by hand.
        hidden = conn.execute(
            f"""SELECT COUNT(*), COALESCE(SUM(pc.member_count), 0)
               FROM place_clusters pc
               WHERE pc.root_id=? AND pc.member_count < ? AND NOT {_PLACE_EXEMPT}""",
            (root_id, min_media)).fetchone()
        members = conn.execute(
            """SELECT pcm.cluster_id, pcm.file_id
               FROM place_cluster_members pcm
               JOIN place_clusters pc ON pc.id=pcm.cluster_id
               WHERE pc.root_id=? ORDER BY pcm.cluster_id, pcm.file_id""",
            (root_id,)).fetchall()
        thumbs: dict[int, list] = {}
        for m in members:
            ids = thumbs.setdefault(m["cluster_id"], [])
            if len(ids) < 4:
                ids.append(m["file_id"])
        return {"clusters": [{
            "id": c["id"], "name": c["name"], "lat": c["lat"], "lon": c["lon"],
            "count": c["member_count"], "thumb_ids": thumbs.get(c["id"], []),
        } for c in clusters], "hidden": {"places": hidden[0], "files": hidden[1]}}
    finally:
        conn.close()


def recompute_place_clusters(db_path: str, root_id: int) -> dict:
    from ..geo.clusters import cluster_places
    conn = db.connect(db_path)
    try:
        stats = cluster_places(conn, root_id)
        return {"clusters": stats.clusters, "points": stats.points}
    finally:
        conn.close()


def place_cluster_members(db_path: str, cluster_id: int, limit=120, offset=0) -> dict | None:
    conn = db.open_readonly(db_path)
    try:
        c = conn.execute(
            "SELECT id, name, lat, lon, member_count FROM place_clusters WHERE id=?",
            (cluster_id,)).fetchone()
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
            (cluster_id, limit, offset)).fetchall()
        return {
            "id": c["id"], "name": c["name"], "lat": c["lat"], "lon": c["lon"],
            "total": c["member_count"],
            "members": [{
                "id": r["id"], "type": r["media_type"],
                "name": os.path.basename(r["rel_path"]),
                "date": r["dt"], "date_source": r["dsrc"],
                "has_gps": bool(r["has_gps"]),
            } for r in rows],
            "offset": offset, "count": len(rows),
            "merges": _place_merges_for(conn, cluster_id),
        }
    finally:
        conn.close()


def _place_merges_for(conn, cluster_id) -> list[dict]:
    """Merges this place can undo. Looked up by survivor_id alone, unlike
    _person_merges_for/_pet_merges_for: place_clusters ids are durable (never
    rebuilt wholesale -- see place_merges' schema comment), so there is no
    "id rotted after a recluster" problem a name-anchor would need to paper
    over."""
    rows = conn.execute(
        """SELECT id, dropped_name, file_ids, created_at FROM place_merges
           WHERE survivor_id=? ORDER BY created_at DESC""",
        (cluster_id,)).fetchall()
    out = []
    for row in rows:
        pairs = json.loads(row["file_ids"])
        out.append({"id": row["id"], "dropped_name": row["dropped_name"],
                    "photos_folded_in": len(pairs), "created_at": row["created_at"]})
    return out


def rename_place_cluster(db_path: str, cluster_id, name: str) -> dict:
    if not cluster_id:
        return {"error": "missing cluster_id"}
    conn = db.connect(db_path)
    try:
        cur = conn.execute(
            "UPDATE place_clusters SET name=? WHERE id=?",
            (name or None, cluster_id))
        conn.commit()
        if cur.rowcount == 0:
            return {"error": "not found"}
        return {"ok": True, "name": name or None}
    finally:
        conn.close()


# -- "same place?" merges (durable, true undo) ------------------------------
#
# Unlike merge_persons/merge_pets, this needs no separate links table: places
# are durable entities (geo/clusters.py's cluster_places is a one-time
# bootstrap, assign_unplaced never deletes a place), so nothing will ever
# silently rebuild place_clusters out from under a merge. That makes undo a
# true restore of the dropped row rather than "delete a constraint and hope
# the next recluster sorts it out" -- see unmerge_place_clusters.

def _place_merge_survivor(pa, pb):
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


def merge_place_clusters(db_path: str, id_a, id_b, name=None) -> dict:
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
    if not id_a or not id_b or id_a == id_b:
        return {"error": "need two distinct places"}
    conn = db.connect(db_path)
    try:
        pa = conn.execute(
            "SELECT id,root_id,name,lat,lon,member_count,pinned FROM place_clusters WHERE id=?",
            (id_a,)).fetchone()
        pb = conn.execute(
            "SELECT id,root_id,name,lat,lon,member_count,pinned FROM place_clusters WHERE id=?",
            (id_b,)).fetchone()
        if not pa or not pb:
            return {"error": "unknown place"}
        if pa["root_id"] != pb["root_id"]:
            return {"error": "cannot merge places from different archives"}
        name = (name or "").strip() or None
        if pa["name"] and pb["name"] and pa["name"] != pb["name"] and not name:
            return {"error": f"both named ({pa['name']} / {pb['name']}); choose a name"}
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
        drop_members = [(r["file_id"], r["source"]) for r in conn.execute(
            "SELECT file_id, source FROM place_cluster_members WHERE cluster_id=?",
            (drop["id"],)).fetchall()]
        survivor_lat_before, survivor_lon_before = keep["lat"], keep["lon"]
        # `OR REPLACE`, not a plain UPDATE: the PK is (cluster_id, file_id),
        # and nothing forbids a file already being a member of BOTH places
        # (e.g. a manual attachment alongside an auto one) -- a plain UPDATE
        # would hit a PK collision on that row instead of just collapsing it.
        conn.execute(
            "UPDATE OR REPLACE place_cluster_members SET cluster_id=? WHERE cluster_id=?",
            (keep["id"], drop["id"]))
        conn.execute("DELETE FROM place_clusters WHERE id=?", (drop["id"],))
        # Coordinates: a pinned survivor's coordinate is a deliberate user
        # pin and never moves. Otherwise, the merged centroid is the
        # member-count weighted mean of the two inputs -- the same
        # incremental-mean rule assign_unplaced uses when it rolls a new
        # point into an existing place (geo/clusters.py:200-207), just
        # applied to two existing centroids instead of one point.
        if not keep["pinned"]:
            n_keep = keep["member_count"] or 0
            n_drop = drop["member_count"] or 0
            total = n_keep + n_drop
            if total:
                new_lat = (keep["lat"] * n_keep + drop["lat"] * n_drop) / total
                new_lon = (keep["lon"] * n_keep + drop["lon"] * n_drop) / total
            else:
                new_lat, new_lon = keep["lat"], keep["lon"]
        else:
            new_lat, new_lon = keep["lat"], keep["lon"]
        # Recompute member_count from place_cluster_members rather than
        # summing the two prior counts: the OR REPLACE above may have
        # collapsed a file that was a member of both, so a plain sum could
        # overcount.
        new_count = conn.execute(
            "SELECT COUNT(*) FROM place_cluster_members WHERE cluster_id=?",
            (keep["id"],)).fetchone()[0]
        survivor_name = name or keep["name"] or drop["name"]
        conn.execute(
            "UPDATE place_clusters SET name=?, lat=?, lon=?, member_count=? WHERE id=?",
            (survivor_name, new_lat, new_lon, new_count, keep["id"]))
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
            (keep["id"], survivor_name, keep["name"], drop["name"],
             drop["lat"], drop["lon"], drop["pinned"],
             survivor_lat_before, survivor_lon_before,
             json.dumps(drop_members), now))
        conn.commit()
        return {"ok": True, "place": {
            "id": keep["id"], "name": survivor_name, "count": new_count}}
    finally:
        conn.close()


def unmerge_place_clusters(db_path: str, merge_id) -> dict:
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
    conn = db.connect(db_path)
    try:
        m = conn.execute(
            "SELECT * FROM place_merges WHERE id=?", (merge_id,)).fetchone()
        if not m:
            return {"error": "merge not found"}
        survivor = conn.execute(
            "SELECT id, root_id, name, member_count FROM place_clusters WHERE id=?",
            (m["survivor_id"],)).fetchone()
        if not survivor:
            return {"error": "survivor place no longer exists"}
        now = db.now_iso()
        cur = conn.execute(
            """INSERT INTO place_clusters(root_id, name, lat, lon, member_count,
                                          pinned, created_at)
               VALUES(?,?,?,?,0,?,?)""",
            (survivor["root_id"], m["dropped_name"], m["dropped_lat"], m["dropped_lon"],
             m["dropped_pinned"], now))
        restored_id = cur.lastrowid
        file_ids = json.loads(m["file_ids"])
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
                (survivor["id"], file_id))
            # INSERT OR REPLACE, not plain INSERT: if the file rejoined the
            # restored side under some other path since the merge, this
            # undo still wins it back for the restored place, matching what
            # the merge itself did to a doubly-attached file.
            conn.execute(
                "INSERT OR REPLACE INTO place_cluster_members(cluster_id, file_id, source) "
                "VALUES(?,?,?)", (restored_id, file_id, source))
        restored_count = conn.execute(
            "SELECT COUNT(*) FROM place_cluster_members WHERE cluster_id=?",
            (restored_id,)).fetchone()[0]
        conn.execute(
            "UPDATE place_clusters SET member_count=? WHERE id=?",
            (restored_count, restored_id))
        # Restore the survivor's pre-merge centroid verbatim (see docstring
        # for why this isn't recomputed) and its member_count from what
        # actually remains after the moves above.
        survivor_count = conn.execute(
            "SELECT COUNT(*) FROM place_cluster_members WHERE cluster_id=?",
            (survivor["id"],)).fetchone()[0]
        # ...and its name, but only if nothing has renamed it since the merge.
        # Merging two differently-named places takes an explicit `name` that
        # OVERWRITES the survivor's own name, so without this the loser's name
        # comes back on the restored place while the survivor keeps the chosen
        # one -- the pre-merge pair "A"/"B" would undo to "A"/"A", quietly
        # losing a name the user had typed. Conditional (not unconditional)
        # for the same reason unmerge_persons guards its manual_person
        # restore: a rename made deliberately after the merge outranks this
        # bookkeeping and must not be clobbered.
        survivor_name = (m["survivor_name_before"]
                         if survivor["name"] == m["survivor_name"] else survivor["name"])
        conn.execute(
            "UPDATE place_clusters SET name=?, lat=?, lon=?, member_count=? WHERE id=?",
            (survivor_name, m["survivor_lat"], m["survivor_lon"], survivor_count,
             survivor["id"]))
        conn.execute("DELETE FROM place_merges WHERE id=?", (merge_id,))
        conn.commit()
        return {"ok": True, "place_id": restored_id}
    finally:
        conn.close()


def place_merge_preview(db_path: str, id_a, id_b, warn_km: float) -> dict:
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
    if not id_a or not id_b or id_a == id_b:
        return {"error": "need two distinct places"}
    conn = db.open_readonly(db_path)
    try:
        pa = conn.execute(
            "SELECT id,root_id,name,lat,lon,member_count,pinned FROM place_clusters WHERE id=?",
            (id_a,)).fetchone()
        pb = conn.execute(
            "SELECT id,root_id,name,lat,lon,member_count,pinned FROM place_clusters WHERE id=?",
            (id_b,)).fetchone()
        if not pa or not pb:
            return {"error": "unknown place"}
        if pa["root_id"] != pb["root_id"]:
            return {"error": "cannot merge places from different archives"}
        keep, drop = _place_merge_survivor(pa, pb)
        # Same rule merge_place_clusters applies when it actually writes the
        # merged row: a pinned survivor's coordinate is a deliberate user pin
        # and never moves; otherwise the centre is the member-count weighted
        # mean of the two centroids (geo/clusters.py's incremental-mean rule,
        # applied to two existing centroids instead of one new point).
        if keep["pinned"]:
            centre_lat, centre_lon = keep["lat"], keep["lon"]
        else:
            n_keep = keep["member_count"] or 0
            n_drop = drop["member_count"] or 0
            total = n_keep + n_drop
            if total:
                centre_lat = (keep["lat"] * n_keep + drop["lat"] * n_drop) / total
                centre_lon = (keep["lon"] * n_keep + drop["lon"] * n_drop) / total
            else:
                centre_lat, centre_lon = keep["lat"], keep["lon"]
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
            (id_a, id_b)).fetchall()
        span_km = 0.0
        for p in points:
            d_km = _haversine_m(centre_lat, centre_lon, p["lat"], p["lon"]) / 1000.0
            if d_km > span_km:
                span_km = d_km
        return {"ok": True, "span_km": span_km, "threshold_km": warn_km,
                "warn": span_km >= warn_km}
    finally:
        conn.close()


# -- media grid + detail ----------------------------------------------------

def media(db_path: str, *, root_id=None, year=None, month=None, mtype=None,
          person_id=None, person_ids=None, cluster_id=None, sort="newest",
          limit=120, offset=0) -> dict:
    conn = db.open_readonly(db_path)
    try:
        where = [_NOT_HIDDEN]
        params: list = []
        rc, rp = _root_clause(root_id)
        if rc:
            where.append(rc.lstrip(" AND "))
            params += rp
        if mtype:
            where.append("f.media_type = ?"); params.append(mtype)
        if year:
            where.append("substr(d.best_datetime,1,4) = ?"); params.append(str(year))
        if month:
            where.append("substr(d.best_datetime,1,7) = ?"); params.append(month)
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
        selected_people = list(dict.fromkeys(
            person_ids or ([person_id] if person_id else [])))
        for selected_person in selected_people:
            # UNION of two `SELECT file_id` subqueries inside the same IN, so
            # manually tagged files (person_files, for media with no detected
            # face) match the filter too, without reshaping this into the
            # correlated-EXISTS shape the comment above warns off.
            where.append(
                f"f.id IN (SELECT fa.file_id FROM faces fa WHERE fa.person_id=? "
                f"AND {_QUALITY_OK} "
                "UNION SELECT pf.file_id FROM person_files pf WHERE pf.person_id=?)")
            params.append(selected_person)
            params.append(selected_person)
        if cluster_id:
            where.append(
                "f.id IN (SELECT pcm.file_id FROM place_cluster_members pcm "
                "WHERE pcm.cluster_id=?)")
            params.append(cluster_id)
        clause = " AND ".join(where)
        total = conn.execute(
            f"""SELECT COUNT(*)
                FROM files f LEFT JOIN dates d ON d.file_id=f.id
                WHERE {clause}""", params).fetchone()[0]
        rows = conn.execute(
            f"""SELECT f.id, f.media_type, f.rel_path, d.best_datetime AS dt,
                       d.date_source AS dsrc,
                       EXISTS(SELECT 1 FROM geo g WHERE g.file_id=f.id) AS has_gps
                FROM files f LEFT JOIN dates d ON d.file_id=f.id
                WHERE {clause}
                ORDER BY (d.best_datetime IS NULL),
                         d.best_datetime {'ASC' if sort == 'oldest' else 'DESC'}, f.id
                LIMIT ? OFFSET ?""", (*params, limit, offset)).fetchall()
        items = [{
            "id": r["id"], "type": r["media_type"],
            "name": os.path.basename(r["rel_path"]),
            "date": r["dt"], "date_source": r["dsrc"],
            "has_gps": bool(r["has_gps"]),
        } for r in rows]
        return {
            "items": items, "offset": offset, "limit": limit,
            "count": len(items), "total": total,
        }
    finally:
        conn.close()


def browse_filters(db_path: str, root_id=None) -> dict:
    """Options for the Browse filter bar: which year/months, media types, named
    people and named places actually occur in this archive. Scoped to the
    default browse view (_NOT_HIDDEN), so the choices match what the grid shows.

    Only *named* people/places are offered, an unnamed auto-cluster is not a
    label a user would filter by, and naming is how they become meaningful."""
    conn = db.open_readonly(db_path)
    try:
        rc, rp = _root_clause(root_id)
        periods = [r[0] for r in conn.execute(
            f"""SELECT DISTINCT substr(d.best_datetime,1,7) p
                FROM files f JOIN dates d ON d.file_id=f.id
                WHERE {_NOT_HIDDEN}{rc} AND d.best_datetime IS NOT NULL
                ORDER BY p DESC""", rp)]
        types = [r[0] for r in conn.execute(
            f"""SELECT DISTINCT media_type FROM files f
                WHERE {_NOT_HIDDEN}{rc} ORDER BY media_type""", rp)]
        people = [{"id": r["id"], "name": r["name"]} for r in conn.execute(
            f"""SELECT p.id, p.name, COUNT(DISTINCT fa.file_id) c
                FROM persons p
                JOIN faces fa ON fa.person_id=p.id
                JOIN files f ON f.id=fa.file_id
                WHERE p.name IS NOT NULL AND {_NOT_HIDDEN} AND {_QUALITY_OK}{rc}
                GROUP BY p.id ORDER BY p.name COLLATE NOCASE""", rp)]
        if root_id is None:
            place_rows = conn.execute(
                """SELECT id, name FROM place_clusters
                   WHERE name IS NOT NULL ORDER BY name COLLATE NOCASE""")
        else:
            place_rows = conn.execute(
                """SELECT id, name FROM place_clusters
                   WHERE name IS NOT NULL AND root_id=?
                   ORDER BY name COLLATE NOCASE""", (root_id,))
        places = [{"id": r["id"], "name": r["name"]} for r in place_rows]
        return {"periods": periods, "types": types,
                "people": people, "places": places}
    finally:
        conn.close()


def folders(db_path: str, root_id: int | None, limit: int = 120) -> dict:
    """Return the archive's source tree as a compact, browseable folder list."""
    conn = db.open_readonly(db_path)
    try:
        rc, rp = _root_clause(root_id)
        rows = conn.execute(
            f"SELECT rel_path FROM files f WHERE {_NOT_HIDDEN}{rc}", rp).fetchall()
        grouped: dict[str, int] = {}
        for row in rows:
            folder = os.path.dirname(row["rel_path"]) or "/"
            grouped[folder] = grouped.get(folder, 0) + 1
        items = sorted(grouped.items(), key=lambda x: (-x[1], x[0].lower()))[:limit]
        return {"folders": [{"path": p, "count": c} for p, c in items]}
    finally:
        conn.close()


# -- semantic Browse search --------------------------------------------------

def semantic_summary(db_path: str, root_id=None) -> dict:
    """Index state for the Browse semantic-search controls."""
    from . import semantic
    conn = db.open_readonly(db_path)
    try:
        rc, rp = _root_clause(root_id)
        total = conn.execute(
            f"""SELECT COUNT(*) FROM files f WHERE {_NOT_HIDDEN}{rc}
                AND f.media_type IN ('image','video','audio','document')""", rp).fetchone()[0]
        rows = conn.execute(
            f"""SELECT e.status, COUNT(*) c FROM semantic_embeddings e
                JOIN files f ON f.id=e.file_id
                WHERE {_NOT_HIDDEN}{rc} AND e.source_sha256=f.sha256
                  AND COALESCE(e.indexer_version, '')=?
                GROUP BY e.status""", (*rp, semantic.INDEXER_VERSION)).fetchall()
        counts = {r["status"]: r["c"] for r in rows}
        completed = sum(counts.values())
        # Per media-type tally of files that actually carry a current embedding
        # (status='indexed'), so Browse can report its search reach as
        # "N images, M videos" rather than a single opaque total.
        type_rows = conn.execute(
            f"""SELECT f.media_type mt, COUNT(*) c FROM semantic_embeddings e
                JOIN files f ON f.id=e.file_id
                WHERE {_NOT_HIDDEN}{rc} AND e.source_sha256=f.sha256
                  AND COALESCE(e.indexer_version, '')=? AND e.status='indexed'
                GROUP BY f.media_type ORDER BY c DESC""",
            (*rp, semantic.INDEXER_VERSION)).fetchall()
        by_type = [{"type": r["mt"], "count": r["c"]} for r in type_rows]
        return {
            "total": total, "indexed": counts.get("indexed", 0),
            "skipped": counts.get("skipped", 0), "errors": counts.get("error", 0),
            "pending": max(0, total - completed), "by_type": by_type,
        }
    finally:
        conn.close()


def semantic_pending(db_path: str, root_id=None) -> int:
    """Compatible media that needs a first (or revised) semantic embedding."""
    from . import semantic
    conn = db.open_readonly(db_path)
    try:
        rc, rp = _root_clause(root_id)
        return conn.execute(
            f"""SELECT COUNT(*) FROM files f
                LEFT JOIN semantic_embeddings e ON e.file_id=f.id
                WHERE {_NOT_HIDDEN}{rc}
                  AND (f.media_type='image' OR f.ext IN ('mp4','mov','mp3','wav','pdf'))
                  AND (e.file_id IS NULL OR e.source_sha256 IS NOT f.sha256
                       OR COALESCE(e.indexer_version, '') != ?)""",
            (*rp, semantic.INDEXER_VERSION),
        ).fetchone()[0]
    finally:
        conn.close()


def semantic_search(db_path: str, query_vector: list[float], *, root_id=None,
                    year=None, month=None, mtype=None, person_id=None, person_ids=None,
                    cluster_id=None, min_similarity=-1.0, sort="relevance",
                    limit=120, offset=0, alternate_vectors=None) -> dict:
    """Rank locally stored vectors against the original and optional expansions.

    Alternate vectors are ``(vector, penalty)`` pairs.  Taking the best score
    preserves strong matches from either wording; the small penalty keeps the
    words the user actually typed ahead when both formulations are equivalent.
    Voyage is called only for the query vectors.
    """
    conn = db.open_readonly(db_path)
    try:
        where = [_NOT_HIDDEN,
                 "e.status='indexed'", "e.embedding IS NOT NULL"]
        params: list = []
        rc, rp = _root_clause(root_id)
        if rc:
            where.append(rc.lstrip(" AND ")); params += rp
        if mtype:
            where.append("f.media_type=?"); params.append(mtype)
        if year:
            where.append("substr(d.best_datetime,1,4)=?"); params.append(str(year))
        if month:
            where.append("substr(d.best_datetime,1,7)=?"); params.append(month)
        selected_people = list(dict.fromkeys(
            person_ids or ([person_id] if person_id else [])))
        for selected_person in selected_people:
            # Mirrors media()'s person filter: union in person_files so
            # manually tagged files (no detected face) match too.
            where.append(
                f"f.id IN (SELECT file_id FROM faces WHERE person_id=? "
                f"AND {_quality_ok('faces')} "
                "UNION SELECT file_id FROM person_files WHERE person_id=?)")
            params.append(selected_person)
            params.append(selected_person)
        if cluster_id:
            where.append("f.id IN (SELECT file_id FROM place_cluster_members WHERE cluster_id=?)")
            params.append(cluster_id)
        rows = conn.execute(
            f"""SELECT f.id, f.media_type, f.rel_path, d.best_datetime AS dt,
                       d.date_source AS dsrc,
                       EXISTS(SELECT 1 FROM geo g WHERE g.file_id=f.id) AS has_gps,
                       e.embedding
                FROM semantic_embeddings e JOIN files f ON f.id=e.file_id
                LEFT JOIN dates d ON d.file_id=f.id
                WHERE {' AND '.join(where)}""", params).fetchall()
        vectors = [(tuple(query_vector), 0.0)]
        vectors.extend((tuple(vector), float(penalty))
                       for vector, penalty in (alternate_vectors or []))
        prepared = [
            (vector, math.sqrt(math.sumprod(vector, vector)), penalty)
            for vector, penalty in vectors
        ]
        prepared = [item for item in prepared if item[1]]
        if not prepared:
            return {"items": [], "offset": offset, "limit": limit,
                    "count": 0, "total": 0}
        ranked = []
        for row in rows:
            try:
                vector = struct.unpack(f"<{len(prepared[0][0])}f", row["embedding"])
            except struct.error:
                continue
            vector_norm = math.sqrt(math.sumprod(vector, vector))
            if not vector_norm:
                continue
            score = max(
                math.sumprod(query, vector) / (query_norm * vector_norm) - penalty
                for query, query_norm, penalty in prepared
            )
            if score >= min_similarity:
                ranked.append((score, row))
        # Which matched is decided by similarity; how they are ordered is the
        # caller's choice.  A date sort still shows only what cleared
        # min_similarity -- it reorders the same result set, never widens it.
        # Undated files sort last either way, as in media().
        if sort == "oldest":
            ranked.sort(key=lambda x: (x[1]["dt"] is None, x[1]["dt"] or "", x[1]["id"]))
        elif sort == "newest":
            ranked.sort(key=lambda x: (x[1]["dt"] or "", -x[1]["id"]), reverse=True)
        else:
            ranked.sort(key=lambda x: x[0], reverse=True)
        page = ranked[offset:offset + limit]
        items = [{
            "id": row["id"], "type": row["media_type"],
            "name": os.path.basename(row["rel_path"]), "date": row["dt"],
            "date_source": row["dsrc"], "has_gps": bool(row["has_gps"]),
            "score": round(score, 4),
        } for score, row in page]
        return {"items": items, "offset": offset, "limit": limit,
                "count": len(items), "total": len(ranked)}
    finally:
        conn.close()


def dup_summary(db_path: str, root_id=None) -> dict:
    conn = db.open_readonly(db_path)
    try:
        rc, rp = _root_clause(root_id)
        row = conn.execute(
            f"""SELECT COUNT(*) groups,
                       COALESCE(SUM(g.member_count-1),0) dups,
                       COALESCE(SUM(g.redundant_bytes),0) bytes
                FROM dup_groups g
                JOIN files f ON f.id=g.canonical_file_id
                WHERE 1=1{rc}""", rp).fetchone()
        return {"groups": row["groups"], "duplicates": row["dups"],
                "reclaimable": row["bytes"]}
    finally:
        conn.close()


def dup_groups(db_path: str, root_id=None, limit=60, offset=0) -> dict:
    conn = db.open_readonly(db_path)
    try:
        rc, rp = _root_clause(root_id)
        groups = conn.execute(
            f"""SELECT g.id, g.method, g.member_count, g.size_each, g.redundant_bytes,
                       g.canonical_file_id
                FROM dup_groups g JOIN files f ON f.id=g.canonical_file_id
                WHERE 1=1{rc}
                ORDER BY g.redundant_bytes DESC, g.id
                LIMIT ? OFFSET ?""", (*rp, limit, offset)).fetchall()
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
                (g["canonical_file_id"], g["id"])).fetchall()
            out.append({
                "id": g["id"], "method": g["method"], "count": g["member_count"],
                "size_each": g["size_each"], "reclaimable": g["redundant_bytes"],
                "canonical_id": g["canonical_file_id"],
                "members": [{
                    "id": m["id"], "type": m["media_type"], "role": m["role"],
                    "match_type": m["match_type"],
                    "name": os.path.basename(m["rel_path"]),
                    "folder": os.path.dirname(m["rel_path"]),
                } for m in members],
            })
        return {"groups": out, "offset": offset, "count": len(out)}
    finally:
        conn.close()


# -- faces / people ---------------------------------------------------------

def faces_pending(db_path: str, root_id=None) -> int:
    """Present images not yet face-scanned (DB-only, no disk walk), used by the
    auto-scheduler to decide whether to queue a faces job."""
    conn = db.open_readonly(db_path)
    try:
        rc, rp = _root_clause(root_id)
        # _NOT_HIDDEN, matching faces.extract.pending_count: the face pass only
        # touches canonical images, so counting unscanned duplicates here would
        # make the scheduler queue faces jobs that find nothing to do, forever.
        return conn.execute(
            f"""SELECT COUNT(*) FROM files f
                LEFT JOIN face_scan s ON s.file_id=f.id
                WHERE s.file_id IS NULL AND {_NOT_HIDDEN} AND f.media_type='image'{rc}""",
            rp).fetchone()[0]
    finally:
        conn.close()


# -- pets / non-human detections -------------------------------------------

def pets_pending(db_path: str, root_id=None, model_source=None) -> int:
    conn = db.open_readonly(db_path)
    try:
        rc, rp = _root_clause(root_id)
        model_clause = ""
        params = []
        if model_source is not None:
            model_clause = " OR s.model_source IS NOT ?"
            params.append(model_source)
        params.extend(rp)
        return conn.execute(
            f"""SELECT COUNT(*) FROM files f
                LEFT JOIN pet_scan s ON s.file_id=f.id
                WHERE (s.file_id IS NULL OR s.source_sha256 IS NOT f.sha256
                       {model_clause})
                  AND {_NOT_HIDDEN} AND f.media_type='image'{rc}""",
            params).fetchone()[0]
    finally:
        conn.close()


def detect_pending(db_path: str, root_id=None, model_source=None,
                   detect_video_frames: int = 0) -> int:
    """Present canonical media missing a current face OR pet scan — the fused
    detect stage's backlog (mirrors detect.extract.pending_count). One file is
    'pending' if either detector still owes it work, so the scheduler keeps the
    single detect stage running until both are drained.

    ``detect_video_frames`` mirrors ``cfg.detect_video_frames``: videos only
    count toward the backlog when it is > 0, otherwise the stage would never
    reach "up to date" while video detection is disabled."""
    conn = db.open_readonly(db_path)
    try:
        rc, rp = _root_clause(root_id)
        media_types = "('image','video')" if detect_video_frames > 0 else "('image')"
        params = [model_source, *rp]
        return conn.execute(
            f"""SELECT COUNT(*) FROM files f
                LEFT JOIN face_scan fs ON fs.file_id=f.id
                LEFT JOIN pet_scan ps ON ps.file_id=f.id
                WHERE (fs.file_id IS NULL
                       OR ps.file_id IS NULL
                       OR ps.source_sha256 IS NOT f.sha256
                       OR ps.model_source IS NOT ?)
                  AND {_NOT_HIDDEN} AND f.media_type IN {media_types}{rc}""",
            params).fetchone()[0]
    finally:
        conn.close()


def pet_summary(db_path: str, root_id=None, model_source=None,
                detect_video_frames: int = 0) -> dict:
    from ..pets import backend as pet_backend
    conn = db.open_readonly(db_path)
    try:
        rc, rp = _root_clause(root_id)
        # See face_summary: videos only join the counted population once the
        # detect stage actually processes them.
        media_types = "('image','video')" if detect_video_frames > 0 else "('image')"
        total = conn.execute(
            f"""SELECT COUNT(*) FROM files f WHERE {_NOT_HIDDEN}
                AND f.media_type IN {media_types}{rc}""", rp).fetchone()[0]
        scan_filter = " AND s.source_sha256 IS f.sha256"
        scan_params = list(rp)
        if model_source is not None:
            scan_filter += " AND s.model_source IS ?"
            scan_params.append(model_source)
        scanned = conn.execute(
            f"""SELECT COUNT(*) FROM pet_scan s JOIN files f ON f.id=s.file_id
                WHERE {_NOT_HIDDEN}{rc}{scan_filter}""", scan_params).fetchone()[0]
        detections = conn.execute(
            f"""SELECT COUNT(*) FROM animal_detections a
                JOIN files f ON f.id=a.file_id
                WHERE {_NOT_HIDDEN} AND a.species!='teddy bear'{rc}""", rp
        ).fetchone()[0]
        groups = conn.execute(
            f"""SELECT COUNT(DISTINCT a.pet_id) FROM animal_detections a
                JOIN files f ON f.id=a.file_id
                WHERE {_NOT_HIDDEN} AND a.pet_id IS NOT NULL{rc}""", rp
        ).fetchone()[0]
        nonhuman = conn.execute(
            f"""SELECT COUNT(*) FROM nonhuman_detections n
                JOIN files f ON f.id=n.file_id WHERE {_NOT_HIDDEN}{rc}""", rp
        ).fetchone()[0]
        return {
            "total_images": total, "scanned": scanned,
            "unscanned": max(0, total - scanned),
            "detections": detections, "pets": groups,
            "nonhuman_faces": nonhuman,
            "backend_available": pet_backend.available(),
        }
    finally:
        conn.close()


def pet_groups(db_path: str, root_id=None, limit=120, offset=0) -> dict:
    conn = db.open_readonly(db_path)
    try:
        rc, rp = _root_clause(root_id)
        rows = conn.execute(
            f"""SELECT a.pet_id,p.name,p.species,p.cover_detection_id,
                       COUNT(*) detections,COUNT(DISTINCT a.file_id) photos
                FROM animal_detections a JOIN pets p ON p.id=a.pet_id
                JOIN files f ON f.id=a.file_id
                WHERE {_NOT_HIDDEN}{rc}
                GROUP BY a.pet_id
                ORDER BY CASE WHEN NULLIF(TRIM(p.name),'') IS NULL THEN 1 ELSE 0 END,
                         detections DESC,a.pet_id
                LIMIT ? OFFSET ?""", (*rp, limit, offset)).fetchall()
        # Manual-only tags (pet_files), same treatment as face_persons: a
        # second small query scoped to this page's pet ids, counting only
        # files not already reachable through animal_detections for that pet.
        pids = [row["pet_id"] for row in rows]
        manual_counts: dict[int, int] = {}
        if pids:
            marks = ",".join("?" for _ in pids)
            rc2 = rc.replace("f.root_id", "f2.root_id") if rc else rc
            manual_rows = conn.execute(
                f"""SELECT pf.pet_id pid, COUNT(*) c
                    FROM pet_files pf JOIN files f2 ON f2.id=pf.file_id
                    WHERE pf.pet_id IN ({marks})
                      AND f2.present=1 AND f2.hidden=0{rc2}
                      AND pf.file_id NOT IN (
                          SELECT a2.file_id FROM animal_detections a2
                          WHERE a2.pet_id=pf.pet_id)
                    GROUP BY pf.pet_id""", (*pids, *rp)).fetchall()
            manual_counts = {row["pid"]: row["c"] for row in manual_rows}
        return {"pets": [{
            "id": row["pet_id"], "name": row["name"], "species": row["species"],
            "cover_detection_id": row["cover_detection_id"],
            "detections": row["detections"],
            "photos": row["photos"] + manual_counts.get(row["pet_id"], 0),
        } for row in rows], "offset": offset, "count": len(rows)}
    finally:
        conn.close()


def animal_gallery(db_path: str, root_id=None, limit=120, offset=0,
                   unassigned=False) -> dict:
    conn = db.open_readonly(db_path)
    try:
        rc, rp = _root_clause(root_id)
        un = " AND a.pet_id IS NULL" if unassigned else ""
        rows = conn.execute(
            f"""SELECT a.id detection_id,a.file_id,a.species,a.det_score,
                       f.rel_path,d.best_datetime dt
                FROM animal_detections a JOIN files f ON f.id=a.file_id
                LEFT JOIN dates d ON d.file_id=f.id
                WHERE {_NOT_HIDDEN} AND a.species!='teddy bear'{un}{rc}
                ORDER BY a.det_score DESC,a.id
                LIMIT ? OFFSET ?""", (*rp, limit, offset)).fetchall()
        return {"items": [{
            "detection_id": row["detection_id"], "id": row["file_id"],
            "species": row["species"], "score": row["det_score"],
            "name": os.path.basename(row["rel_path"]), "date": row["dt"],
        } for row in rows], "offset": offset, "count": len(rows)}
    finally:
        conn.close()


def pet_group(db_path: str, pet_id: int, root_id=None, limit=120, offset=0):
    """Files this pet appears in: files with a detection of them, UNION ALL
    files manually tagged with them (pet_files) that don't already have such
    a detection -- mirrors face_person. Manual-only items carry
    detection_id=None."""
    conn = db.open_readonly(db_path)
    try:
        pet = conn.execute(
            "SELECT id,name,species FROM pets WHERE id=?", (pet_id,)).fetchone()
        if not pet:
            return None
        rc, rp = _root_clause(root_id)
        rows = conn.execute(
            f"""SELECT id, rel_path, dt, detection_id FROM (
                    SELECT f.id AS id, f.rel_path, d.best_datetime AS dt,
                           a.id AS detection_id
                    FROM animal_detections a JOIN files f ON f.id=a.file_id
                    LEFT JOIN dates d ON d.file_id=f.id
                    WHERE a.pet_id=? AND {_NOT_HIDDEN}{rc}
                    UNION ALL
                    SELECT f.id AS id, f.rel_path, d.best_datetime AS dt,
                           NULL AS detection_id
                    FROM pet_files pf JOIN files f ON f.id=pf.file_id
                    LEFT JOIN dates d ON d.file_id=f.id
                    WHERE pf.pet_id=? AND {_NOT_HIDDEN}{rc}
                      AND pf.file_id NOT IN (
                          SELECT a2.file_id FROM animal_detections a2
                          WHERE a2.pet_id=?)
                )
                ORDER BY (dt IS NULL),dt DESC,id
                LIMIT ? OFFSET ?""",
            (pet_id, *rp, pet_id, *rp, pet_id, limit, offset)).fetchall()
        total = conn.execute(
            f"""SELECT
                    (SELECT COUNT(DISTINCT a.file_id) FROM animal_detections a
                     JOIN files f ON f.id=a.file_id
                     WHERE a.pet_id=? AND {_NOT_HIDDEN}{rc})
                  + (SELECT COUNT(*) FROM pet_files pf
                     JOIN files f ON f.id=pf.file_id
                     WHERE pf.pet_id=? AND {_NOT_HIDDEN}{rc}
                       AND pf.file_id NOT IN (
                           SELECT a2.file_id FROM animal_detections a2
                           WHERE a2.pet_id=?))""",
            (pet_id, *rp, pet_id, *rp, pet_id)).fetchone()[0]
        return {
            "id": pet["id"], "name": pet["name"], "species": pet["species"],
            "photos": total,
            "items": [{"id": row["id"], "name": os.path.basename(row["rel_path"]),
                       "date": row["dt"], "detection_id": row["detection_id"],
                       "type": "image", "has_gps": False}
                      for row in rows],
            "offset": offset, "count": len(rows),
            "merges": _pet_merges_for(conn, pet_id, pet["name"]),
        }
    finally:
        conn.close()


def _pet_merges_for(conn, pet_id, name) -> list[dict]:
    """Merges this pet can undo. Looked up by survivor_id OR by survivor_name
    (when non-empty), mirroring _person_merges_for: cluster_pets rebuilds
    `pets` wholesale after every detect chunk, so a merge's survivor_id can
    rot -- the name is the durable anchor."""
    name = name or ""
    rows = conn.execute(
        """SELECT id, dropped_name, det_ids, created_at FROM pet_merges
           WHERE survivor_id=? OR (? != '' AND survivor_name=?)
           ORDER BY created_at DESC""",
        (pet_id, name, name)).fetchall()
    out = []
    for row in rows:
        dids = json.loads(row["det_ids"])
        photos = 0
        if dids:
            marks = ",".join("?" for _ in dids)
            photos = conn.execute(
                f"SELECT COUNT(DISTINCT file_id) FROM animal_detections WHERE id IN ({marks})",
                dids).fetchone()[0]
        out.append({"id": row["id"], "dropped_name": row["dropped_name"],
                    "photos_folded_in": photos, "created_at": row["created_at"]})
    return out


def rename_pet(db_path: str, pet_id, name: str) -> dict:
    conn = db.connect(db_path)
    try:
        if not conn.execute("SELECT 1 FROM pets WHERE id=?", (pet_id,)).fetchone():
            return {"error": "unknown pet"}
        conn.execute("UPDATE pets SET name=? WHERE id=?", (name or None, pet_id))
        conn.commit()
        return {"ok": True, "name": name or None}
    finally:
        conn.close()


def nonhuman_review(db_path: str, root_id=None, limit=120, offset=0) -> dict:
    conn = db.open_readonly(db_path)
    try:
        rc, rp = _root_clause(root_id)
        rows = conn.execute(
            f"""SELECT n.id,n.file_id,n.kind,n.confidence,n.source,n.review_status,
                       n.box_x,n.box_y,n.box_w,n.box_h,f.rel_path
                FROM nonhuman_detections n JOIN files f ON f.id=n.file_id
                WHERE {_NOT_HIDDEN}{rc}
                ORDER BY n.confidence DESC,n.id LIMIT ? OFFSET ?""",
            (*rp, limit, offset)).fetchall()
        total = conn.execute(
            f"""SELECT COUNT(*) FROM nonhuman_detections n
                JOIN files f ON f.id=n.file_id WHERE {_NOT_HIDDEN}{rc}""",
            rp).fetchone()[0]
        return {"items": [dict(row) for row in rows], "total": total,
                "offset": offset, "count": len(rows)}
    finally:
        conn.close()


def review_nonhuman(db_path: str, detection_id, verdict: str) -> dict:
    """Confirm a non-human candidate or restore it to People as unassigned."""
    if verdict not in {"confirmed", "human"}:
        return {"error": "verdict must be confirmed or human"}
    conn = db.connect(db_path)
    try:
        row = conn.execute(
            """SELECT n.*,f.root_id FROM nonhuman_detections n
               JOIN files f ON f.id=n.file_id WHERE n.id=?""", (detection_id,)
        ).fetchone()
        if not row:
            return {"error": "unknown non-human detection"}
        if verdict == "confirmed":
            conn.execute(
                "UPDATE nonhuman_detections SET review_status='confirmed' WHERE id=?",
                (detection_id,))
            conn.commit()
            return {"ok": True, "status": "confirmed", "root_id": row["root_id"]}
        if row["restored_face_id"]:
            conn.execute(
                """UPDATE faces SET not_person=0,nonhuman_kind=NULL,
                                    nonhuman_source=NULL
                   WHERE id=?""", (row["restored_face_id"],))
            conn.execute(
                "UPDATE nonhuman_detections SET review_status='human' WHERE id=?",
                (detection_id,))
            conn.execute(
                """UPDATE face_scan SET n_faces=n_faces+1,
                   rejected_nonhuman=MAX(0,rejected_nonhuman-1)
                   WHERE file_id=?""", (row["file_id"],))
            conn.commit()
            return {"ok": True, "status": "human", "root_id": row["root_id"],
                    "face_id": row["restored_face_id"]}
        if not row["embedding"]:
            return {"error": "candidate has no retained embedding; rescan is required"}
        cursor = conn.execute(
            """INSERT INTO faces
               (file_id,box_x,box_y,box_w,box_h,det_score,focus_score,brightness,
                extreme_fraction,clipped_fraction,quality_score,quality_source,
                embedding,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row["file_id"], row["box_x"], row["box_y"], row["box_w"], row["box_h"],
             row["det_score"], row["focus_score"], row["brightness"],
             row["extreme_fraction"], row["clipped_fraction"], row["quality_score"],
             row["quality_source"], row["embedding"], db.now_iso()))
        conn.execute(
            """UPDATE nonhuman_detections
               SET review_status='human',restored_face_id=? WHERE id=?""",
            (cursor.lastrowid, detection_id))
        conn.execute(
            """UPDATE face_scan SET n_faces=n_faces+1,
                   rejected_nonhuman=MAX(0,rejected_nonhuman-1)
               WHERE file_id=?""", (row["file_id"],))
        conn.commit()
        return {"ok": True, "status": "human", "root_id": row["root_id"],
                "face_id": cursor.lastrowid}
    finally:
        conn.close()


def face_summary(db_path: str, root_id=None, detect_video_frames: int = 0) -> dict:
    from ..faces import backend as fb
    conn = db.open_readonly(db_path)
    try:
        rc, rp = _root_clause(root_id)
        # Face detection only runs on canonical (non-duplicate) media, so every
        # count here is over _NOT_HIDDEN, the "total" is unique media, matching
        # the people grid and what actually gets scanned. Videos only count
        # once the detect stage actually processes them (detect_video_frames >
        # 0) — otherwise this would claim videos are "unscanned" forever even
        # though the stage deliberately skips them while the feature is off.
        media_types = "('image','video')" if detect_video_frames > 0 else "('image')"
        total_images = conn.execute(
            f"SELECT COUNT(*) FROM files f WHERE {_NOT_HIDDEN} AND f.media_type IN {media_types}{rc}",
            rp).fetchone()[0]
        scanned = conn.execute(
            f"""SELECT COUNT(*) FROM files f JOIN face_scan s ON s.file_id=f.id
                WHERE {_NOT_HIDDEN} AND f.media_type IN {media_types}{rc}""", rp).fetchone()[0]
        faces = conn.execute(
            f"""SELECT COUNT(*) FROM faces fa JOIN files f ON f.id=fa.file_id
                WHERE {_NOT_HIDDEN} AND fa.not_person=0 AND {_QUALITY_OK}{rc}""", rp).fetchone()[0]
        people = conn.execute(
            f"""SELECT COUNT(DISTINCT fa.person_id) FROM faces fa
                JOIN files f ON f.id=fa.file_id
                WHERE {_NOT_HIDDEN} AND fa.person_id IS NOT NULL AND {_QUALITY_OK}{rc}""", rp).fetchone()[0]
        photos_with_faces = conn.execute(
            f"""SELECT COUNT(DISTINCT fa.file_id) FROM faces fa
                JOIN files f ON f.id=fa.file_id
                WHERE {_NOT_HIDDEN} AND fa.not_person=0 AND {_QUALITY_OK}{rc}""", rp).fetchone()[0]
        return {
            "total_images": total_images, "scanned": scanned,
            "unscanned": max(0, total_images - scanned),
            "faces": faces, "people": people,
            "photos_with_faces": photos_with_faces,
            "backend_available": fb.available(),
        }
    finally:
        conn.close()


def _preview_faces(conn, pids, k=4) -> dict:
    """Up to k sharpest (highest det_score), non-hidden face ids per person, for
    the 4-up collage on each person card. One window-function query for the page."""
    pids = [p for p in pids if p is not None]
    if not pids:
        return {}
    marks = ",".join("?" * len(pids))
    rows = conn.execute(
        f"""SELECT person_id, id FROM (
                SELECT fa.id, fa.person_id,
                       ROW_NUMBER() OVER (PARTITION BY fa.person_id
                                          ORDER BY fa.det_score DESC, fa.id) rn
                FROM faces fa JOIN files f ON f.id=fa.file_id
                WHERE fa.person_id IN ({marks}) AND f.hidden=0 AND {_QUALITY_OK}
            ) WHERE rn <= ?""", (*pids, k)).fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r["person_id"], []).append(r["id"])
    return out


def face_persons(db_path: str, root_id=None, limit=120, offset=0) -> dict:
    """People (clusters) in this archive, named people first and then most faces.
    Each carries up to 4 preview faces for a collage card + photo/face counts."""
    conn = db.open_readonly(db_path)
    try:
        rc, rp = _root_clause(root_id)
        rows = conn.execute(
            f"""SELECT fa.person_id pid, p.name, p.cover_face_id,
                       COUNT(DISTINCT fa.file_id) photos, COUNT(*) faces
                FROM faces fa JOIN files f ON f.id=fa.file_id
                JOIN persons p ON p.id=fa.person_id
                WHERE {_NOT_HIDDEN} AND fa.person_id IS NOT NULL AND {_QUALITY_OK}{rc}
                GROUP BY fa.person_id
                ORDER BY CASE WHEN NULLIF(TRIM(p.name), '') IS NULL THEN 1 ELSE 0 END,
                         faces DESC, pid
                LIMIT ? OFFSET ?""", (*rp, limit, offset)).fetchall()
        prev = _preview_faces(conn, [r["pid"] for r in rows])
        # Manual-only tags (person_files) aren't reachable from the faces JOIN
        # above, so a person's card would undercount photos for anyone also
        # tagged by hand on a file with no detected face of them. Rather than
        # reshape the main query (see the long comment in media() about why a
        # correlated EXISTS over this archive is a trap), do a second small
        # query scoped to just this page's person ids, counting only files
        # NOT already reachable through faces for that same person so a file
        # tagged both ways still counts once.
        pids = [r["pid"] for r in rows]
        manual_counts: dict[int, int] = {}
        if pids:
            marks = ",".join("?" for _ in pids)
            rc2 = rc.replace("f.root_id", "f2.root_id") if rc else rc
            manual_rows = conn.execute(
                f"""SELECT pf.person_id pid, COUNT(*) c
                    FROM person_files pf JOIN files f2 ON f2.id=pf.file_id
                    WHERE pf.person_id IN ({marks})
                      AND f2.present=1 AND f2.hidden=0{rc2}
                      AND pf.file_id NOT IN (
                          SELECT fa2.file_id FROM faces fa2
                          WHERE fa2.person_id=pf.person_id)
                    GROUP BY pf.person_id""", (*pids, *rp)).fetchall()
            manual_counts = {r["pid"]: r["c"] for r in manual_rows}
        people = [{
            "id": r["pid"], "name": r["name"], "cover_face_id": r["cover_face_id"],
            "faces_preview": prev.get(r["pid"], []),
            "photos": r["photos"] + manual_counts.get(r["pid"], 0),
            "faces": r["faces"],
        } for r in rows]
        return {"people": people, "offset": offset, "count": len(people)}
    finally:
        conn.close()


def face_person(db_path: str, person_id: int, root_id=None,
                limit=120, offset=0) -> dict | None:
    """Files this person appears in: files with a detected face of them,
    UNION ALL files manually tagged with them (person_files) that don't
    already have such a face -- so a file tagged both ways appears once, and
    manual-only items carry face_id=None."""
    conn = db.open_readonly(db_path)
    try:
        p = conn.execute(
            "SELECT id, name FROM persons WHERE id=?", (person_id,)).fetchone()
        if not p:
            return None
        rc, rp = _root_clause(root_id)
        rows = conn.execute(
            f"""SELECT id, media_type, rel_path, dt, dsrc, has_gps, face_id FROM (
                    SELECT f.id AS id, f.media_type, f.rel_path,
                           d.best_datetime AS dt, d.date_source AS dsrc,
                           EXISTS(SELECT 1 FROM geo g WHERE g.file_id=f.id) AS has_gps,
                           (SELECT fa2.id FROM faces fa2
                            WHERE fa2.file_id=f.id AND fa2.person_id=?
                              AND {_quality_ok("fa2")}
                            ORDER BY fa2.det_score DESC LIMIT 1) AS face_id
                    FROM faces fa JOIN files f ON f.id=fa.file_id
                    LEFT JOIN dates d ON d.file_id=f.id
                    WHERE fa.person_id=? AND {_NOT_HIDDEN} AND {_QUALITY_OK}{rc}
                    GROUP BY f.id
                    UNION ALL
                    SELECT f.id AS id, f.media_type, f.rel_path,
                           d.best_datetime AS dt, d.date_source AS dsrc,
                           EXISTS(SELECT 1 FROM geo g WHERE g.file_id=f.id) AS has_gps,
                           NULL AS face_id
                    FROM person_files pf JOIN files f ON f.id=pf.file_id
                    LEFT JOIN dates d ON d.file_id=f.id
                    WHERE pf.person_id=? AND {_NOT_HIDDEN}{rc}
                      AND pf.file_id NOT IN (
                          SELECT fa2.file_id FROM faces fa2 WHERE fa2.person_id=?)
                )
                ORDER BY (dt IS NULL), dt DESC, id
                LIMIT ? OFFSET ?""",
            (person_id, person_id, *rp, person_id, *rp, person_id,
             limit, offset)).fetchall()
        total = conn.execute(
            f"""SELECT
                    (SELECT COUNT(DISTINCT fa.file_id) FROM faces fa
                     JOIN files f ON f.id=fa.file_id
                     WHERE fa.person_id=? AND {_NOT_HIDDEN} AND {_QUALITY_OK}{rc})
                  + (SELECT COUNT(*) FROM person_files pf
                     JOIN files f ON f.id=pf.file_id
                     WHERE pf.person_id=? AND {_NOT_HIDDEN}{rc}
                       AND pf.file_id NOT IN (
                           SELECT fa2.file_id FROM faces fa2 WHERE fa2.person_id=?))""",
            (person_id, *rp, person_id, *rp, person_id)).fetchone()[0]
        items = [{
            "id": r["id"], "type": r["media_type"],
            "name": os.path.basename(r["rel_path"]),
            "date": r["dt"], "date_source": r["dsrc"],
            "has_gps": bool(r["has_gps"]), "face_id": r["face_id"],
        } for r in rows]
        return {"id": person_id, "name": p["name"], "photos": total,
                "items": items, "offset": offset, "count": len(items),
                "merges": _person_merges_for(conn, person_id, p["name"])}
    finally:
        conn.close()


def _person_merges_for(conn, person_id, name) -> list[dict]:
    """Merges this person can undo. Looked up by survivor_id OR by
    survivor_name (when non-empty) rather than survivor_id alone: cluster_faces
    DELETEs and rebuilds every `persons` row, so a merge's survivor_id can
    rot after a recluster -- the name is the durable anchor, same reasoning
    as person_files (see repair_manual_person_files)."""
    name = name or ""
    rows = conn.execute(
        """SELECT id, dropped_name, face_ids, created_at FROM person_merges
           WHERE survivor_id=? OR (? != '' AND survivor_name=?)
           ORDER BY created_at DESC""",
        (person_id, name, name)).fetchall()
    out = []
    for row in rows:
        fids = json.loads(row["face_ids"])
        photos = 0
        if fids:
            marks = ",".join("?" for _ in fids)
            photos = conn.execute(
                f"SELECT COUNT(DISTINCT file_id) FROM faces WHERE id IN ({marks})",
                fids).fetchone()[0]
        out.append({"id": row["id"], "dropped_name": row["dropped_name"],
                    "photos_folded_in": photos, "created_at": row["created_at"]})
    return out


def rename_person(db_path: str, person_id, name: str) -> dict:
    if not person_id:
        return {"error": "missing person_id"}
    conn = db.connect(db_path)
    try:
        old = conn.execute(
            "SELECT name FROM persons WHERE id=?", (person_id,)).fetchone()
        if not old:
            return {"error": "not found"}
        conn.execute("UPDATE persons SET name=? WHERE id=?", (name or None, person_id))
        # Keep manual face-pins (stored by NAME) tracking the rename so re-clustering
        # still lands pinned faces on this person.
        if old["name"] and name and old["name"] != name:
            conn.execute("UPDATE faces SET manual_person=? WHERE manual_person=?",
                         (name, old["name"]))
        conn.commit()
        return {"ok": True, "name": name or None}
    finally:
        conn.close()


# -- in-panel edits (date / face / place) -----------------------------------

def set_date(db_path: str, file_id, value: str) -> dict:
    """Set a manual, variable-precision date. `value` is a YYYY, YYYY-MM or
    YYYY-MM-DD prefix (stored as-is; the whole app groups/sorts by prefix)."""
    if not file_id:
        return {"error": "missing file_id"}
    v = (value or "").strip()
    parts = v.split("-")
    ok = False
    try:
        if len(parts) == 1 and len(parts[0]) == 4:
            y = int(parts[0]); ok = 1 <= y <= 9999
        elif len(parts) == 2:
            y, mo = int(parts[0]), int(parts[1])
            ok = 1 <= y <= 9999 and 1 <= mo <= 12 and len(parts[1]) == 2
        elif len(parts) == 3:
            y, mo, da = int(parts[0]), int(parts[1]), int(parts[2])
            ok = (1 <= y <= 9999 and 1 <= mo <= 12 and 1 <= da <= 31
                  and len(parts[1]) == 2 and len(parts[2]) == 2)
    except ValueError:
        ok = False
    if not ok:
        return {"error": "date must be YYYY, YYYY-MM or YYYY-MM-DD"}
    conn = db.connect(db_path)
    try:
        if not conn.execute("SELECT 1 FROM files WHERE id=?", (file_id,)).fetchone():
            return {"error": "unknown file"}
        conn.execute(
            """INSERT OR REPLACE INTO dates(file_id, best_datetime, date_source,
                                            date_confidence)
               VALUES(?,?, 'manual', 1.0)""", (file_id, v))
        conn.commit()
        return {"ok": True, "date": v, "date_source": "manual"}
    finally:
        conn.close()


def _sync_person_stats(conn, pid):
    """Recompute one person's face_count + cover after a face moves in/out; drop
    it if it's now empty. Mirrors faces/cluster.py's _refresh_person_stats."""
    if pid is None:
        return
    left = conn.execute(
        f"SELECT COUNT(*) FROM faces fa JOIN files f ON f.id=fa.file_id "
        f"WHERE fa.person_id=? AND f.hidden=0 AND {_QUALITY_OK}", (pid,)).fetchone()[0]
    if left == 0 and not conn.execute(
            "SELECT 1 FROM faces WHERE person_id=?", (pid,)).fetchone():
        conn.execute("DELETE FROM persons WHERE id=?", (pid,))
        return
    cover = conn.execute(
        f"SELECT fa.id FROM faces fa JOIN files f ON f.id=fa.file_id "
        f"WHERE fa.person_id=? AND f.hidden=0 AND {_QUALITY_OK} "
        f"ORDER BY fa.det_score DESC LIMIT 1",
        (pid,)).fetchone()
    conn.execute("UPDATE persons SET face_count=?, cover_face_id=? WHERE id=?",
                 (left, cover["id"] if cover else None, pid))


def reassign_face(db_path: str, face_id, person_id) -> dict:
    """Move a face to a named person and PIN it (by name) so re-clustering keeps
    it there. Only named persons are valid targets."""
    if not face_id or not person_id:
        return {"error": "missing face_id or person_id"}
    conn = db.connect(db_path)
    try:
        fa = conn.execute(
            "SELECT person_id FROM faces WHERE id=?", (face_id,)).fetchone()
        if not fa:
            return {"error": "unknown face"}
        p = conn.execute(
            "SELECT id, name FROM persons WHERE id=?", (person_id,)).fetchone()
        if not p or not p["name"]:
            return {"error": "target must be a named person"}
        old_pid = fa["person_id"]
        conn.execute("UPDATE faces SET person_id=?, manual_person=? WHERE id=?",
                     (person_id, p["name"], face_id))
        _sync_person_stats(conn, person_id)
        if old_pid and old_pid != person_id:
            _sync_person_stats(conn, old_pid)
        conn.commit()
        return {"ok": True, "person": {"id": p["id"], "name": p["name"]}}
    finally:
        conn.close()


def detach_file_from_person(db_path: str, person_id, file_id) -> dict:
    """"This photo isn't them": release every face of this file currently
    assigned to person_id, and durably block them from drifting back.

    Each detached face is unassigned (person_id and manual_person cleared)
    and pinned with a cannot-link (face_links 'different') against the
    person's representative face, so a later recluster can't quietly re-add
    it -- same mechanism unmerge_persons uses, just against a single face
    instead of a whole cluster.

    Also drops any person_files manual tag for this (person, file) pair: a
    manual tag added earlier today would otherwise keep the photo showing on
    the person's page despite the detach (see person_files' schema comment)."""
    if not person_id or not file_id:
        return {"error": "missing person_id or file_id"}
    conn = db.connect(db_path)
    try:
        p = conn.execute(
            "SELECT id, cover_face_id FROM persons WHERE id=?", (person_id,)).fetchone()
        if not p:
            return {"error": "unknown person"}
        faces = conn.execute(
            "SELECT id FROM faces WHERE file_id=? AND person_id=?",
            (file_id, person_id)).fetchall()
        if not faces:
            return {"error": "this file has no face assigned to that person"}
        rep = _rep_face(conn, person_id, p["cover_face_id"])
        now = db.now_iso()
        face_ids = [r["id"] for r in faces]
        for fid in face_ids:
            _record_link(conn, rep, fid, "different", now)
        marks = ",".join("?" for _ in face_ids)
        conn.execute(
            f"UPDATE faces SET person_id=NULL, manual_person=NULL WHERE id IN ({marks})",
            face_ids)
        conn.execute(
            "DELETE FROM person_files WHERE person_id=? AND file_id=?",
            (person_id, file_id))
        _sync_person_stats(conn, person_id)
        _update_person_centroid(conn, person_id)
        conn.commit()
        return {"ok": True, "detached_faces": len(face_ids)}
    finally:
        conn.close()


def add_person_to_file(db_path: str, person_id, file_id) -> dict:
    """Tag a file with a named person by hand, for media where no face was
    detected at all. Only named persons are valid targets (mirrors
    reassign_face) -- an unnamed auto-cluster id is ephemeral and wouldn't
    survive the next re-cluster anyway."""
    if not person_id or not file_id:
        return {"error": "missing person_id or file_id"}
    conn = db.connect(db_path)
    try:
        p = conn.execute(
            "SELECT id, name FROM persons WHERE id=?", (person_id,)).fetchone()
        if not p or not p["name"]:
            return {"error": "target must be a named person"}
        if not conn.execute("SELECT 1 FROM files WHERE id=?", (file_id,)).fetchone():
            return {"error": "unknown file"}
        conn.execute(
            """INSERT OR REPLACE INTO person_files(person_id, file_id, person_name, created_at)
               VALUES(?,?,?,?)""", (person_id, file_id, p["name"], db.now_iso()))
        conn.commit()
        return {"ok": True, "person": {"id": p["id"], "name": p["name"]}}
    finally:
        conn.close()


def remove_person_from_file(db_path: str, person_id, file_id) -> dict:
    if not person_id or not file_id:
        return {"error": "missing person_id or file_id"}
    conn = db.connect(db_path)
    try:
        conn.execute(
            "DELETE FROM person_files WHERE person_id=? AND file_id=?",
            (person_id, file_id))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


def add_pet_to_file(db_path: str, pet_id, file_id) -> dict:
    """Tag a file with a named pet by hand. Same shape as add_person_to_file,
    against pet_files/pets."""
    if not pet_id or not file_id:
        return {"error": "missing pet_id or file_id"}
    conn = db.connect(db_path)
    try:
        p = conn.execute(
            "SELECT id, name FROM pets WHERE id=?", (pet_id,)).fetchone()
        if not p or not p["name"]:
            return {"error": "target must be a named pet"}
        if not conn.execute("SELECT 1 FROM files WHERE id=?", (file_id,)).fetchone():
            return {"error": "unknown file"}
        conn.execute(
            """INSERT OR REPLACE INTO pet_files(pet_id, file_id, pet_name, created_at)
               VALUES(?,?,?,?)""", (pet_id, file_id, p["name"], db.now_iso()))
        conn.commit()
        return {"ok": True, "pet": {"id": p["id"], "name": p["name"]}}
    finally:
        conn.close()


def remove_pet_from_file(db_path: str, pet_id, file_id) -> dict:
    if not pet_id or not file_id:
        return {"error": "missing pet_id or file_id"}
    conn = db.connect(db_path)
    try:
        conn.execute(
            "DELETE FROM pet_files WHERE pet_id=? AND file_id=?", (pet_id, file_id))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


def repair_manual_person_files(conn) -> None:
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
    rows = conn.execute(
        "SELECT person_id, file_id, person_name FROM person_files").fetchall()
    for row in rows:
        name_row = conn.execute(
            "SELECT name FROM persons WHERE id=?", (row["person_id"],)).fetchone()
        if name_row and name_row["name"] == row["person_name"]:
            continue  # still points at a person carrying the anchored name
        target = conn.execute(
            "SELECT id FROM persons WHERE name=?", (row["person_name"],)).fetchone()
        if not target:
            continue  # name doesn't exist right now; leave the row alone
        if target["id"] == row["person_id"]:
            continue
        conn.execute(
            "DELETE FROM person_files WHERE person_id=? AND file_id=?",
            (target["id"], row["file_id"]))
        conn.execute(
            "UPDATE person_files SET person_id=? WHERE person_id=? AND file_id=?",
            (target["id"], row["person_id"], row["file_id"]))


def repair_manual_pet_files(conn) -> None:
    """Re-point manual pet_files rows onto whatever pet id currently carries
    the stored name, after cluster_pets has rebuilt `pets`.

    Called from pets/cluster.py before every commit/return -- unlike the
    person version this isn't defensive, it's mandatory: pet ids are
    guaranteed to change on every detect chunk (cluster_pets DELETEs and
    rebuilds `pets` wholesale each time). See repair_manual_person_files for
    the "leave untouched if the name isn't currently held" and
    primary-key-collision handling, which are identical here.
    """
    rows = conn.execute(
        "SELECT pet_id, file_id, pet_name, created_at FROM pet_files").fetchall()
    for row in rows:
        name_row = conn.execute(
            "SELECT name FROM pets WHERE id=?", (row["pet_id"],)).fetchone()
        if name_row and name_row["name"] == row["pet_name"]:
            continue
        target = conn.execute(
            "SELECT id FROM pets WHERE name=?", (row["pet_name"],)).fetchone()
        if not target:
            continue  # name doesn't exist right now; leave the row alone
        if target["id"] == row["pet_id"]:
            continue
        conn.execute(
            "DELETE FROM pet_files WHERE pet_id=? AND file_id=?",
            (target["id"], row["file_id"]))
        conn.execute(
            "UPDATE pet_files SET pet_id=? WHERE pet_id=? AND file_id=?",
            (target["id"], row["pet_id"], row["file_id"]))


# -- "same person?" review: merges + constraints ---------------------------

def _rep_face(conn, pid, cover) -> int | None:
    """A stable representative face id for a person (its cover, or its sharpest
    face). Used to anchor a durable face_links constraint."""
    if cover:
        return cover
    r = conn.execute(
        f"SELECT id FROM faces WHERE person_id=? AND {_quality_ok('faces')} "
        f"ORDER BY det_score DESC LIMIT 1", (pid,)).fetchone()
    return r["id"] if r else None


def _rep_detection(conn, pid, cover) -> int | None:
    """A stable representative animal_detection id for a pet (its cover, or
    its highest-scoring detection). Mirrors _rep_face; used to anchor a
    durable pet_links constraint, which -- unlike a pet id -- survives
    cluster_pets' per-chunk DELETE/rebuild of the `pets` table."""
    if cover:
        return cover
    r = conn.execute(
        "SELECT id FROM animal_detections WHERE pet_id=? ORDER BY det_score DESC LIMIT 1",
        (pid,)).fetchone()
    return r["id"] if r else None


def _record_link(conn, fa, fb, kind: str, now: str) -> tuple[int, int] | None:
    """Write a durable face_links constraint. Returns the (sorted) pair it
    wrote, or None if there was nothing to link -- callers that need to record
    exactly which link a merge produced (person_merges.link_a/link_b, for
    undo) key off this return value rather than re-deriving the sort."""
    if not fa or not fb or fa == fb:
        return None
    a, b = sorted((fa, fb))
    conn.execute("INSERT OR REPLACE INTO face_links(face_a, face_b, kind, created_at) "
                 "VALUES(?,?,?,?)", (a, b, kind, now))
    return (a, b)


def _update_person_centroid(conn, pid) -> None:
    import numpy as np
    rows = conn.execute(
        f"SELECT fa.embedding e FROM faces fa JOIN files f ON f.id=fa.file_id "
        f"WHERE fa.person_id=? AND f.hidden=0 AND {_QUALITY_OK}", (pid,)).fetchall()
    if not rows:
        return
    X = np.array([np.frombuffer(r["e"], "float32") for r in rows], dtype="float32")
    X /= np.linalg.norm(X, axis=1, keepdims=True) + 1e-9
    c = X.mean(0)
    c = (c / (np.linalg.norm(c) + 1e-9)).astype("float32")
    conn.execute("UPDATE persons SET centroid=? WHERE id=?", (c.tobytes(), pid))


def merge_persons(db_path: str, id_a, id_b, name=None) -> dict:
    """User confirmed two clusters are the same person. Merge immediately (move
    faces, keep the named/larger one) AND store a durable 'same' constraint so
    the merge survives future re-clusters.

    An explicit `name` picks the surviving NAME outright (it doesn't change
    which person id survives -- that's still named/larger-cluster/id as
    below). This is what lets two already-but-differently-named clusters
    merge at all: normally that's refused because there's no automatic way to
    choose between them."""
    if not id_a or not id_b or id_a == id_b:
        return {"error": "need two distinct persons"}
    conn = db.connect(db_path)
    try:
        pa = conn.execute("SELECT id,name,cover_face_id,face_count FROM persons WHERE id=?",
                          (id_a,)).fetchone()
        pb = conn.execute("SELECT id,name,cover_face_id,face_count FROM persons WHERE id=?",
                          (id_b,)).fetchone()
        if not pa or not pb:
            return {"error": "unknown person"}
        name = (name or "").strip() or None
        if pa["name"] and pb["name"] and pa["name"] != pb["name"] and not name:
            return {"error": f"both named ({pa['name']} / {pb['name']}); choose a name"}
        # keep the named one, else the larger cluster
        if pa["name"] and not pb["name"]:
            keep, drop = pa, pb
        elif pb["name"] and not pa["name"]:
            keep, drop = pb, pa
        elif (pa["face_count"] or 0) >= (pb["face_count"] or 0):
            keep, drop = pa, pb
        else:
            keep, drop = pb, pa
        now = db.now_iso()
        # Captured before the UPDATE below moves them: this is exactly the set
        # unmerge_persons needs to know which faces to hand back to the losing
        # side (see person_merges.face_ids).
        drop_face_ids = [r[0] for r in conn.execute(
            "SELECT id FROM faces WHERE person_id=?", (drop["id"],)).fetchall()]
        link = _record_link(conn, _rep_face(conn, keep["id"], keep["cover_face_id"]),
                            _rep_face(conn, drop["id"], drop["cover_face_id"]), "same", now)
        conn.execute("UPDATE faces SET person_id=? WHERE person_id=?", (keep["id"], drop["id"]))
        conn.execute("DELETE FROM persons WHERE id=?", (drop["id"],))
        survivor_name = name or keep["name"]
        if survivor_name:
            conn.execute("UPDATE persons SET name=? WHERE id=?", (survivor_name, keep["id"]))
            # Load-bearing, not cosmetic: faces/cluster.py's _apply_manual_pins
            # recreates a person from any face still pinned (by NAME) to a
            # name that person no longer carries. Without this, a face that
            # was pinned to the LOSING name (e.g. the drop side of an
            # explicit-name merge of two differently-named clusters) would
            # still hold that stale manual_person and silently resurrect the
            # just-merged-away person on the very next recluster.
            conn.execute(
                "UPDATE faces SET manual_person=? WHERE person_id=? "
                "AND manual_person IS NOT NULL AND manual_person != ''",
                (survivor_name, keep["id"]))
        _sync_person_stats(conn, keep["id"])
        _update_person_centroid(conn, keep["id"])
        # Recorded so this merge can be undone later (see unmerge_persons):
        # which faces moved, what the losing side was called, and which
        # face_links row to retract.
        conn.execute(
            """INSERT INTO person_merges(survivor_id, survivor_name, dropped_name,
                                          face_ids, link_a, link_b, created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (keep["id"], survivor_name, drop["name"], json.dumps(drop_face_ids),
             link[0] if link else None, link[1] if link else None, now))
        conn.commit()
        r = conn.execute("SELECT id,name,face_count FROM persons WHERE id=?", (keep["id"],)).fetchone()
        return {"ok": True, "person": {"id": r["id"], "name": r["name"], "face_count": r["face_count"]}}
    finally:
        conn.close()


def unmerge_persons(db_path: str, merge_id) -> dict:
    """Undo a drag-merge recorded by merge_persons.

    Deleting the 'same' face_links row it wrote is not enough on its own: if
    the automatic clusterer would group these two faces back together anyway,
    they'd silently re-merge on the very next recluster. So this also writes
    a 'different' (cannot-link) row over the same pair -- _apply_links gives
    cannot-link precedence over must-link, and it stays reversible (merging
    them again writes 'same' back over it via INSERT OR REPLACE).

    It also restores names: any recorded face whose manual_person currently
    equals the survivor's name is pinned back to the dropped name (or NULL
    if the dropped side was unnamed), or faces/cluster.py's _apply_manual_pins
    would keep re-pinning it onto the survivor forever. See merge_persons'
    "Load-bearing, not cosmetic" comment -- this undoes exactly that.

    Known limitation: _apply_links only blocks the automatic pass from
    UNIONING two of its own clusters; it never SPLITS a cluster the automatic
    pass forms on its own. If the clusterer's embeddings alone would already
    place these faces in one cluster, this cannot-link can't pull them back
    apart -- that's pre-existing clustering behaviour, not something undo
    can fix.

    Safe to call twice: a stale/missing merge_id returns a clean error
    rather than raising. Returns {"ok": True, "recluster": True} on success
    so the caller knows to kick off a re-cluster (nothing here moves faces
    back itself -- that's the recluster's job, honouring the restored pins
    and the new cannot-link)."""
    if not merge_id:
        return {"error": "missing merge_id"}
    conn = db.connect(db_path)
    try:
        m = conn.execute(
            "SELECT * FROM person_merges WHERE id=?", (merge_id,)).fetchone()
        if not m:
            return {"error": "merge not found"}
        now = db.now_iso()
        if m["link_a"] and m["link_b"]:
            conn.execute(
                "DELETE FROM face_links WHERE face_a=? AND face_b=?",
                (m["link_a"], m["link_b"]))
            conn.execute(
                "INSERT OR REPLACE INTO face_links(face_a, face_b, kind, created_at) "
                "VALUES(?,?,'different',?)", (m["link_a"], m["link_b"], now))
        survivor_name = m["survivor_name"]
        dropped_name = m["dropped_name"] or None
        face_ids = json.loads(m["face_ids"])
        if face_ids and survivor_name:
            marks = ",".join("?" for _ in face_ids)
            conn.execute(
                f"UPDATE faces SET manual_person=? WHERE id IN ({marks}) "
                f"AND manual_person=?", (dropped_name, *face_ids, survivor_name))
        conn.execute("DELETE FROM person_merges WHERE id=?", (merge_id,))
        conn.commit()
        return {"ok": True, "recluster": True}
    finally:
        conn.close()


# -- "same pet?" merges (durable) -------------------------------------------
#
# pets/cluster.py:cluster_pets DELETEs and rebuilds the whole `pets` table
# after every detect chunk, so a naive merge (just moving detections between
# pet ids) would be undone within a minute. `pet_links`, anchored to
# animal_detections ids rather than pet ids, is the constraint that survives
# that rebuild -- see pets/cluster.py's `_apply_links` for the clustering
# side of this, and faces/cluster.py's `_apply_links` / `face_links` for the
# original version of the same trick.

def _record_pet_link(conn, det_a, det_b, now: str) -> tuple[int, int] | None:
    """Write a durable 'same' pet_links constraint. Returns the (sorted) pair
    it wrote, or None if there was nothing to link -- mirrors _record_link;
    used by merge_pets to fill in pet_merges.link_a/link_b for undo."""
    if not det_a or not det_b or det_a == det_b:
        return None
    a, b = sorted((det_a, det_b))
    conn.execute(
        "INSERT OR REPLACE INTO pet_links(det_a, det_b, kind, created_at) "
        "VALUES(?,?,'same',?)", (a, b, now))
    return (a, b)


def _refresh_pet_stats(conn, pet_id, name) -> None:
    """Recompute a pet's aggregate fields from its current detections and set
    its name. Used right after merge_pets moves detections into the surviving
    pet, so a merged pet ends up with the same shape cluster_pets would have
    produced had it clustered this combined group directly."""
    import numpy as np
    rows = conn.execute(
        "SELECT id, species, det_score, embedding FROM animal_detections WHERE pet_id=?",
        (pet_id,)).fetchall()
    cover = max(rows, key=lambda r: r["det_score"]) if rows else None
    # Majority species among the merged detections, ties broken by the
    # best-scoring detection among the tied species -- the same rule
    # cluster_pets applies to a pet_links-merged group, so a merge and the
    # rebuild that follows it agree (a dog once misdetected as a cat merges in).
    species = None
    if rows:
        counts = Counter(r["species"] for r in rows)
        top = max(counts.values())
        tied = {sp for sp, c in counts.items() if c == top}
        species = max((r for r in rows if r["species"] in tied),
                      key=lambda r: r["det_score"])["species"]
    emb_rows = [r for r in rows if r["embedding"]]
    centroid = None
    if emb_rows:
        V = np.array([np.frombuffer(r["embedding"], "float32") for r in emb_rows],
                     dtype="float32")
        V /= np.linalg.norm(V, axis=1, keepdims=True) + 1e-9
        c = V.mean(axis=0)
        c = (c / (np.linalg.norm(c) + 1e-9)).astype("float32")
        centroid = c.tobytes()
    conn.execute(
        """UPDATE pets SET name=?, species=?, cover_detection_id=?,
                          detection_count=?, centroid=? WHERE id=?""",
        (name, species, cover["id"] if cover else None, len(rows), centroid, pet_id))


def merge_pets(db_path: str, id_a, id_b, name=None) -> dict:
    """User confirmed two pet clusters are the same animal. Merge immediately
    (move detections, keep the named/larger one) AND store a durable 'same'
    pet_links constraint so the merge survives the next cluster_pets rebuild.
    Mirrors merge_persons; see its docstring for the explicit-`name` override."""
    if not id_a or not id_b or id_a == id_b:
        return {"error": "need two distinct pets"}
    conn = db.connect(db_path)
    try:
        pa = conn.execute(
            "SELECT id,name,cover_detection_id,detection_count FROM pets WHERE id=?",
            (id_a,)).fetchone()
        pb = conn.execute(
            "SELECT id,name,cover_detection_id,detection_count FROM pets WHERE id=?",
            (id_b,)).fetchone()
        if not pa or not pb:
            return {"error": "unknown pet"}
        name = (name or "").strip() or None
        if pa["name"] and pb["name"] and pa["name"] != pb["name"] and not name:
            return {"error": f"both named ({pa['name']} / {pb['name']}); choose a name"}
        # Survivor: the named one, else the larger detection_count, else the
        # lower id (deterministic tiebreak so repeated merges are stable).
        if pa["name"] and not pb["name"]:
            keep, drop = pa, pb
        elif pb["name"] and not pa["name"]:
            keep, drop = pb, pa
        elif (pa["detection_count"] or 0) != (pb["detection_count"] or 0):
            keep, drop = (pa, pb) if (pa["detection_count"] or 0) > (pb["detection_count"] or 0) else (pb, pa)
        elif pa["id"] < pb["id"]:
            keep, drop = pa, pb
        else:
            keep, drop = pb, pa
        now = db.now_iso()
        # Captured before the UPDATE below moves them, mirroring merge_persons
        # -- unmerge_pets needs to know which detections this merge folded in.
        drop_det_ids = [r[0] for r in conn.execute(
            "SELECT id FROM animal_detections WHERE pet_id=?", (drop["id"],)).fetchall()]
        link = _record_pet_link(conn, _rep_detection(conn, keep["id"], keep["cover_detection_id"]),
                                _rep_detection(conn, drop["id"], drop["cover_detection_id"]), now)
        # foreign_keys=ON (see db.connect): animal_detections.pet_id is
        # ON DELETE SET NULL, so the UPDATE moving drop's detections MUST run
        # before DELETE FROM pets, or they'd be orphaned instead of merged.
        conn.execute("UPDATE animal_detections SET pet_id=? WHERE pet_id=?",
                     (keep["id"], drop["id"]))
        conn.execute("DELETE FROM pets WHERE id=?", (drop["id"],))
        survivor_name = name or keep["name"] or drop["name"]
        _refresh_pet_stats(conn, keep["id"], survivor_name)
        # Recorded so this merge can be undone later (see unmerge_pets).
        conn.execute(
            """INSERT INTO pet_merges(survivor_id, survivor_name, dropped_name,
                                       det_ids, link_a, link_b, created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (keep["id"], survivor_name, drop["name"], json.dumps(drop_det_ids),
             link[0] if link else None, link[1] if link else None, now))
        conn.commit()
        r = conn.execute(
            "SELECT id,name,species,detection_count FROM pets WHERE id=?",
            (keep["id"],)).fetchone()
        return {"ok": True, "pet": {
            "id": r["id"], "name": r["name"], "species": r["species"],
            "detections": r["detection_count"]}}
    finally:
        conn.close()


def unmerge_pets(db_path: str, merge_id) -> dict:
    """Undo a drag-merge recorded by merge_pets. Mirrors unmerge_persons: the
    recorded 'same' pet_links row is deleted and replaced with a 'different'
    (cannot-link) row over the same detection pair, so cluster_pets' next
    rebuild can't silently re-form the merge (pets/cluster.py's _apply_links
    now honours 'different' the same way faces/cluster.py's does).

    Simpler than the person version: pet names aren't pinned per-detection
    (animal_detections.manual_pet exists in the schema but nothing writes to
    it yet), so there's no name pin to restore -- cluster_pets' own
    name-carryover (best overlap with a still-named pet) sorts the dropped
    name back out on the next rebuild by itself.

    Safe to call twice: a stale/missing merge_id returns a clean error rather
    than raising. Returns {"ok": True, "recluster": True} so the caller knows
    to kick off cluster_pets."""
    if not merge_id:
        return {"error": "missing merge_id"}
    conn = db.connect(db_path)
    try:
        m = conn.execute(
            "SELECT * FROM pet_merges WHERE id=?", (merge_id,)).fetchone()
        if not m:
            return {"error": "merge not found"}
        now = db.now_iso()
        if m["link_a"] and m["link_b"]:
            conn.execute(
                "DELETE FROM pet_links WHERE det_a=? AND det_b=?",
                (m["link_a"], m["link_b"]))
            conn.execute(
                "INSERT OR REPLACE INTO pet_links(det_a, det_b, kind, created_at) "
                "VALUES(?,?,'different',?)", (m["link_a"], m["link_b"], now))
        conn.execute("DELETE FROM pet_merges WHERE id=?", (merge_id,))
        conn.commit()
        return {"ok": True, "recluster": True}
    finally:
        conn.close()


def _persons_link(db_path: str, id_a, id_b, kind: str) -> dict:
    """Record a durable pairwise constraint between two clusters (by their
    representative faces). 'different' = cannot-link (blocks future auto-merge);
    'skip' = "reviewed, undecided" (just drops the pair from the queue so it stops
    coming back). Neither changes the current clustering."""
    if not id_a or not id_b or id_a == id_b:
        return {"error": "need two distinct persons"}
    conn = db.connect(db_path)
    try:
        pa = conn.execute("SELECT id,cover_face_id FROM persons WHERE id=?", (id_a,)).fetchone()
        pb = conn.execute("SELECT id,cover_face_id FROM persons WHERE id=?", (id_b,)).fetchone()
        if not pa or not pb:
            return {"error": "unknown person"}
        _record_link(conn, _rep_face(conn, pa["id"], pa["cover_face_id"]),
                     _rep_face(conn, pb["id"], pb["cover_face_id"]), kind, db.now_iso())
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


def set_persons_different(db_path: str, id_a, id_b) -> dict:
    return _persons_link(db_path, id_a, id_b, "different")


def set_persons_skip(db_path: str, id_a, id_b) -> dict:
    return _persons_link(db_path, id_a, id_b, "skip")


def hide_person(db_path: str, person_id, kind="false_detection") -> dict:
    """User marked a cluster as NOT a person (a doll / animal / cartoon face that
    YuNet detected). Flag its faces so they're excluded from every future cluster,
    then drop the person. Durable and reversible only by clearing not_person."""
    if not person_id:
        return {"error": "missing person_id"}
    conn = db.connect(db_path)
    try:
        if not conn.execute("SELECT 1 FROM persons WHERE id=?", (person_id,)).fetchone():
            return {"error": "unknown person"}
        allowed = {"animal", "toy", "cartoon", "false_detection"}
        kind = kind if kind in allowed else "false_detection"
        conn.execute(
            """UPDATE faces SET not_person=1,person_id=NULL,
                                nonhuman_kind=?,nonhuman_source='manual'
               WHERE person_id=?""", (kind, person_id))
        conn.execute("DELETE FROM persons WHERE id=?", (person_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


def person_suggestions(db_path: str, root_id=None, limit=40, min_sim=0.45) -> dict:
    """Top candidate "same person?" pairs: distinct clusters whose centroids are
    >= min_sim cosine, highest first, excluding pairs the user already answered
    'different'. This is the review queue, the pairs the automatic pass left
    apart but that look like they could be one person."""
    import numpy as np
    conn = db.open_readonly(db_path)
    try:
        rows = conn.execute(
            "SELECT id,name,cover_face_id,face_count,centroid FROM persons "
            "WHERE centroid IS NOT NULL AND face_count > 0").fetchall()
        if len(rows) < 2:
            return {"suggestions": []}
        ids = [r["id"] for r in rows]
        C = np.array([np.frombuffer(r["centroid"], "float32") for r in rows], dtype="float32")
        C /= np.linalg.norm(C, axis=1, keepdims=True) + 1e-9
        S = C @ C.T
        iu = np.triu_indices(len(ids), 1)
        sims = S[iu]
        keep = sims >= min_sim
        ii, jj, ss = iu[0][keep], iu[1][keep], sims[keep]
        order = np.argsort(-ss)
        # person-pairs the user already answered 'different' (cannot-link) or
        # 'skip' (reviewed, undecided), both are removed from the queue so it
        # stops resurfacing the same pairs.
        excl = set()
        for lk in conn.execute(
                "SELECT face_a,face_b FROM face_links WHERE kind IN ('different','skip')"):
            fa = conn.execute("SELECT person_id FROM faces WHERE id=?", (lk["face_a"],)).fetchone()
            fb = conn.execute("SELECT person_id FROM faces WHERE id=?", (lk["face_b"],)).fetchone()
            if fa and fb and fa[0] and fb[0]:
                excl.add(frozenset((fa[0], fb[0])))
        info = {r["id"]: r for r in rows}
        out, total = [], 0
        for o in order:
            ia, ib = ids[int(ii[o])], ids[int(jj[o])]
            if frozenset((ia, ib)) in excl:
                continue
            total += 1                      # count every un-answered candidate
            if len(out) >= limit:
                continue
            ra, rb = info[ia], info[ib]
            out.append({
                "sim": round(float(ss[o]), 3),
                "a": {"id": ia, "name": ra["name"], "cover_face_id": ra["cover_face_id"],
                      "faces": ra["face_count"]},
                "b": {"id": ib, "name": rb["name"], "cover_face_id": rb["cover_face_id"],
                      "faces": rb["face_count"]},
            })
        prev = _preview_faces(conn, [x["a"]["id"] for x in out] + [x["b"]["id"] for x in out])
        for x in out:
            x["a"]["faces_preview"] = prev.get(x["a"]["id"], [])
            x["b"]["faces_preview"] = prev.get(x["b"]["id"], [])
        return {"suggestions": out, "total": total}
    finally:
        conn.close()


def set_place(db_path: str, file_id, place_id) -> dict:
    """Attach a file to a place as a MANUAL member (membership only, no geo is
    written, so provenance stays honest). Replaces any existing membership."""
    if not file_id or not place_id:
        return {"error": "missing file_id or place_id"}
    conn = db.connect(db_path)
    try:
        pc = conn.execute(
            "SELECT id, name FROM place_clusters WHERE id=?", (place_id,)).fetchone()
        if not pc:
            return {"error": "unknown place"}
        _detach_file_from_places(conn, file_id)
        conn.execute(
            "INSERT OR IGNORE INTO place_cluster_members(cluster_id, file_id, source) "
            "VALUES(?,?, 'manual')", (place_id, file_id))
        _recount_place(conn, place_id)
        conn.commit()
        return {"ok": True, "place": {"id": pc["id"], "name": pc["name"]}}
    finally:
        conn.close()


def clear_place(db_path: str, file_id) -> dict:
    if not file_id:
        return {"error": "missing file_id"}
    conn = db.connect(db_path)
    try:
        _detach_file_from_places(conn, file_id)
        conn.commit()
        return {"ok": True, "place": None}
    finally:
        conn.close()


def create_place(db_path: str, root_id, name: str, lat, lon, file_id=None) -> dict:
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
    conn = db.connect(db_path)
    try:
        if not conn.execute("SELECT 1 FROM roots WHERE id=?", (root_id,)).fetchone():
            return {"error": "unknown archive"}
        cur = conn.execute(
            """INSERT INTO place_clusters(root_id, name, lat, lon, member_count,
                                          pinned, created_at)
               VALUES(?,?,?,?,0,1,?)""",
            (root_id, (name or "").strip() or None, lat, lon, db.now_iso()))
        cid = cur.lastrowid
        if file_id:
            _detach_file_from_places(conn, file_id)
            conn.execute(
                "INSERT OR IGNORE INTO place_cluster_members(cluster_id, file_id, "
                "source) VALUES(?,?, 'manual')", (cid, file_id))
            _recount_place(conn, cid)
        conn.commit()
        pc = conn.execute(
            "SELECT id, name FROM place_clusters WHERE id=?", (cid,)).fetchone()
        return {"ok": True, "id": cid, "place": {"id": pc["id"], "name": pc["name"]}}
    finally:
        conn.close()


def _detach_file_from_places(conn, file_id):
    affected = [r["cluster_id"] for r in conn.execute(
        "SELECT cluster_id FROM place_cluster_members WHERE file_id=?", (file_id,))]
    conn.execute("DELETE FROM place_cluster_members WHERE file_id=?", (file_id,))
    for cid in affected:
        _recount_place(conn, cid)


def _recount_place(conn, cluster_id):
    n = conn.execute(
        "SELECT COUNT(*) FROM place_cluster_members WHERE cluster_id=?",
        (cluster_id,)).fetchone()[0]
    conn.execute("UPDATE place_clusters SET member_count=? WHERE id=?",
                 (n, cluster_id))


def face_crop_source(db_path: str, face_id: int):
    """(abs path, sha256, box, rotate_deg, frame_offset, media_type, file_id)
    for a face id, or None. ``file_id`` is the underlying file's id, distinct
    from ``face_id``, for keying a re-derived video frame the same way
    regardless of which face on that frame asked for it.

    Path is DB-derived. ``rotate_deg`` is the turn the photo needs on top of its
    EXIF orientation — the box was detected in that rotated frame, so a crop has
    to rotate before it cuts. ``frame_offset`` is the ffmpeg ``-ss`` offset of
    the sampled video keyframe this face was detected in, or None for a photo
    (in which case ``rotate_deg`` still applies and ``media_type`` is
    'image'); a video detection's box is instead in that *extracted frame's*
    own pixel coordinates and is already upright (ffmpeg applies container
    rotation on extraction), so callers must not also rotate it."""
    conn = db.open_readonly(db_path)
    try:
        r = conn.execute(
            """SELECT r.path AS root, f.rel_path, f.sha256, f.media_type, f.id AS file_id,
                      fa.box_x, fa.box_y, fa.box_w, fa.box_h, fa.frame_offset,
                      COALESCE(o.rotate_deg, 0) AS rotate_deg
               FROM faces fa JOIN files f ON f.id=fa.file_id
               JOIN roots r ON r.id=f.root_id
               LEFT JOIN orientation o ON o.file_id=f.id
               WHERE fa.id=?""", (face_id,)).fetchone()
        if not r:
            return None
        p = Path(r["root"]) / r["rel_path"]
        if not p.is_file():
            return None
        return (p, r["sha256"],
                (r["box_x"], r["box_y"], r["box_w"], r["box_h"]), r["rotate_deg"],
                r["frame_offset"], r["media_type"], r["file_id"])
    finally:
        conn.close()


def animal_crop_source(db_path: str, detection_id: int):
    """(abs path, sha256, box, rotate_deg, frame_offset, media_type, file_id)
    for an animal detection id. See ``face_crop_source`` for the video-frame
    caveats and what ``file_id`` is for."""
    conn = db.open_readonly(db_path)
    try:
        row = conn.execute(
            """SELECT r.path root,f.rel_path,f.sha256,f.media_type,f.id AS file_id,
                      a.box_x,a.box_y,a.box_w,a.box_h,a.frame_offset,
                      COALESCE(o.rotate_deg, 0) AS rotate_deg
               FROM animal_detections a JOIN files f ON f.id=a.file_id
               JOIN roots r ON r.id=f.root_id
               LEFT JOIN orientation o ON o.file_id=f.id
               WHERE a.id=?""",
            (detection_id,)).fetchone()
        if not row:
            return None
        path = Path(row["root"]) / row["rel_path"]
        if not path.is_file():
            return None
        return (path, row["sha256"], (
            row["box_x"], row["box_y"], row["box_w"], row["box_h"]), row["rotate_deg"],
            row["frame_offset"], row["media_type"], row["file_id"])
    finally:
        conn.close()


def item(db_path: str, fid: int, min_media: int = 10) -> dict | None:
    conn = db.open_readonly(db_path)
    try:
        f = conn.execute(
            """SELECT f.*, r.path AS root_path FROM files f
               JOIN roots r ON r.id=f.root_id WHERE f.id=?""", (fid,)).fetchone()
        if not f:
            return None
        d = conn.execute("SELECT * FROM dates WHERE file_id=?", (fid,)).fetchone()
        g = conn.execute("SELECT * FROM geo WHERE file_id=?", (fid,)).fetchone()
        m = conn.execute("SELECT * FROM media_meta WHERE file_id=?", (fid,)).fetchone()
        t = conn.execute("SELECT * FROM takeout_sidecar WHERE file_id=?", (fid,)).fetchone()
        people = [{
            "person_id": r["person_id"], "name": r["name"], "face_id": r["face_id"],
        } for r in conn.execute(
            f"""SELECT fa.id AS face_id, fa.person_id, p.name
               FROM faces fa LEFT JOIN persons p ON p.id=fa.person_id
               WHERE fa.file_id=? AND fa.not_person=0 AND {_QUALITY_OK}
               ORDER BY fa.det_score DESC""", (fid,))]
        animals = [{
            "detection_id": row["detection_id"], "species": row["species"],
            "pet_id": row["pet_id"], "name": row["name"],
            "score": row["det_score"],
        } for row in conn.execute(
            """SELECT a.id detection_id,a.species,a.pet_id,p.name,a.det_score
               FROM animal_detections a LEFT JOIN pets p ON p.id=a.pet_id
               WHERE a.file_id=? AND a.species!='teddy bear'
               ORDER BY a.det_score DESC""", (fid,))]
        # Current place membership (a file belongs to at most one place).
        # A below-threshold, unnamed/unpinned cluster is not reported as a
        # "place" here either (see place_min_media) — this file just has no
        # location, matching what place_clusters() shows on the map.
        place = conn.execute(
            f"""SELECT pc.id, pc.name FROM place_cluster_members pcm
               JOIN place_clusters pc ON pc.id=pcm.cluster_id
               WHERE pcm.file_id=? AND (pc.member_count >= ? OR {_PLACE_EXEMPT})
               LIMIT 1""", (fid, min_media)).fetchone()
        # Pick-lists for in-panel editing: only *named* places (in this file's root)
        # and *named* persons are offered as targets.
        place_options = [{"id": r["id"], "name": r["name"]} for r in conn.execute(
            """SELECT id, name FROM place_clusters
               WHERE root_id=? AND name IS NOT NULL
               ORDER BY name COLLATE NOCASE""", (f["root_id"],))]
        person_options = [{"id": r["id"], "name": r["name"]} for r in conn.execute(
            """SELECT id, name FROM persons WHERE name IS NOT NULL AND name != ''
               ORDER BY name COLLATE NOCASE""")]
        pet_options = [{"id": r["id"], "name": r["name"]} for r in conn.execute(
            """SELECT id, name FROM pets WHERE name IS NOT NULL AND name != ''
               ORDER BY name COLLATE NOCASE""")]
        # Manually tagged people/pets on this file (person_files/pet_files) --
        # no face/detection exists for these, so there's no face_id/detection_id.
        # Name resolves from the live persons/pets table; if the id has rotted
        # (a re-cluster ran since and repair hasn't caught up yet) fall back to
        # the name stored on the row itself.
        manual_people = [{
            "person_id": r["person_id"], "name": r["name"] or r["person_name"],
        } for r in conn.execute(
            """SELECT pf.person_id, pf.person_name, p.name
               FROM person_files pf LEFT JOIN persons p ON p.id=pf.person_id
               WHERE pf.file_id=?
               ORDER BY COALESCE(p.name, pf.person_name) COLLATE NOCASE""", (fid,))]
        manual_pets = [{
            "pet_id": r["pet_id"], "name": r["name"] or r["pet_name"],
        } for r in conn.execute(
            """SELECT pf.pet_id, pf.pet_name, p.name
               FROM pet_files pf LEFT JOIN pets p ON p.id=pf.pet_id
               WHERE pf.file_id=?
               ORDER BY COALESCE(p.name, pf.pet_name) COLLATE NOCASE""", (fid,))]
        return {
            "id": fid, "name": os.path.basename(f["rel_path"]),
            "rel_path": f["rel_path"], "type": f["media_type"], "size": f["size"],
            "root_id": f["root_id"],
            "date": d["best_datetime"] if d else None,
            "date_source": d["date_source"] if d else None,
            "date_confidence": d["date_confidence"] if d else None,
            "gps": ({"lat": g["lat"], "lon": g["lon"], "alt": g["alt"],
                     "source": g["geo_source"]} if g else None),
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
    finally:
        conn.close()


def media_source(db_path: str, fid: int):
    """(absolute path, content sha256, rotate_deg) for a file id, or None.

    The sha256 is what the thumbnail cache is keyed on, so byte-identical
    duplicates (rife in this cross-takeout archive) share one thumbnail and can
    never disagree. Path is DB-derived, so no request-driven path traversal.
    ``rotate_deg`` is the turn detection found this photo's pixels to be stored
    at, on top of EXIF (0 whenever EXIF alone was right)."""
    conn = db.open_readonly(db_path)
    try:
        r = conn.execute(
            """SELECT r.path AS root, f.rel_path, f.sha256,
                      COALESCE(o.rotate_deg, 0) AS rotate_deg
               FROM files f JOIN roots r ON r.id=f.root_id
               LEFT JOIN orientation o ON o.file_id=f.id
               WHERE f.id=?""", (fid,)).fetchone()
        if not r:
            return None
        p = Path(r["root"]) / r["rel_path"]
        if not p.is_file():
            return None
        return p, r["sha256"], r["rotate_deg"]
    finally:
        conn.close()
