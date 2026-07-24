"""Read-only queries backing the GUI. Each opens its own connection so the
handler stays thread-safe under ThreadingHTTPServer.

Everything is scoped to an archive (a row in `roots`). A root_id of None means
"all archives combined".
"""

from __future__ import annotations

import os
import math
import struct
import json
from pathlib import Path

from ..db import database as db

# Analytics (summary, timeline, map, counts) describe the whole archive, so they
# count every present file. Only Browse hides non-canonical duplicates.
_VISIBLE = "f.present = 1"
_NOT_HIDDEN = "f.present = 1 AND f.hidden = 0"


def _root_clause(root_id):
    if root_id is None:
        return "", []
    return " AND f.root_id = ?", [root_id]


# -- archives ---------------------------------------------------------------

def archives(db_path: str) -> list[dict]:
    conn = db.open_readonly(db_path)
    try:
        out = []
        for r in conn.execute("SELECT id, path, added_at FROM roots ORDER BY id"):
            stats = conn.execute(
                f"""SELECT COUNT(*) c, COALESCE(SUM(size),0) s,
                           SUM(sha256 IS NOT NULL) hashed
                    FROM files f WHERE {_VISIBLE} AND f.root_id=?""",
                (r["id"],),
            ).fetchone()
            dated = conn.execute(
                """SELECT COUNT(*) FROM files f JOIN dates d ON d.file_id=f.id
                   WHERE f.root_id=?""", (r["id"],)
            ).fetchone()[0]
            last = conn.execute(
                "SELECT started_at, finished_at FROM scan_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            out.append({
                "id": r["id"], "path": r["path"],
                "name": os.path.basename(r["path"].rstrip("/")) or r["path"],
                "added_at": r["added_at"],
                "files": stats["c"], "size": stats["s"],
                "hashed": stats["hashed"] or 0,
                "enriched": dated,
                "exists": Path(r["path"]).is_dir(),
                "last_scan": (last["finished_at"] or last["started_at"]) if last else None,
            })
        return out
    finally:
        conn.close()


def add_archive(db_path: str, path: str) -> dict:
    p = Path(path).expanduser()
    if not p.is_dir():
        return {"error": f"Not a directory: {path}"}
    conn = db.connect(db_path)
    try:
        db.init_db(conn)
        rid = db.get_or_create_root(conn, str(p.resolve()))
        return {"id": rid, "path": str(p.resolve())}
    finally:
        conn.close()


def remove_archive(db_path: str, cache_dir: str, root_id: int) -> dict:
    """Forget one archive and its derived data, without touching its originals.

    The catalog database and thumbnail cache are shared by all configured roots.
    Cache entries keyed by a content hash are therefore removed only when no
    remaining file uses that hash.  Model files, icons, and other global cache
    assets intentionally remain in place.
    """
    if not isinstance(root_id, int):
        return {"error": "root_id is required"}
    conn = db.connect(db_path)
    try:
        root = conn.execute("SELECT path FROM roots WHERE id=?", (root_id,)).fetchone()
        if not root:
            return {"error": "archive not found"}
        root_path = root["path"]
        files = conn.execute(
            "SELECT id, sha256 FROM files WHERE root_id=?", (root_id,)
        ).fetchall()
        file_ids = [row["id"] for row in files]
        hashes = {row["sha256"] for row in files if row["sha256"]}
        face_ids = [row[0] for row in conn.execute(
            "SELECT id FROM faces WHERE file_id IN (SELECT id FROM files WHERE root_id=?)",
            (root_id,),
        )]

        # A legacy whole-catalog dedup group can still contain files from another
        # root. Remove that group as a whole and unhide its surviving members;
        # keeping it would leave them pointing at a deleted canonical file.
        group_ids = [row[0] for row in conn.execute(
            """SELECT DISTINCT dm.group_id FROM dup_members dm
               JOIN files f ON f.id=dm.file_id WHERE f.root_id=?""", (root_id,)
        )]
        if group_ids:
            marks = ",".join("?" for _ in group_ids)
            conn.execute(
                f"UPDATE files SET hidden=0, dup_group_id=NULL WHERE dup_group_id IN ({marks})",
                group_ids,
            )
            conn.execute(f"DELETE FROM dup_groups WHERE id IN ({marks})", group_ids)

        # The derived tables attached to files use ON DELETE CASCADE. Persons are
        # global clusters, so prune only clusters left with no faces afterwards.
        conn.execute("DELETE FROM files WHERE root_id=?", (root_id,))
        conn.execute("DELETE FROM persons WHERE NOT EXISTS "
                     "(SELECT 1 FROM faces WHERE faces.person_id=persons.id)")

        # Scan history may cover several roots. Retain those records, minus this
        # root, rather than losing the other archives' history.
        for run in conn.execute("SELECT id, roots FROM scan_runs").fetchall():
            try:
                paths = json.loads(run["roots"] or "[]")
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(paths, list) or root_path not in paths:
                continue
            paths = [p for p in paths if p != root_path]
            if paths:
                conn.execute("UPDATE scan_runs SET roots=? WHERE id=?",
                             (json.dumps(paths), run["id"]))
            else:
                conn.execute("DELETE FROM scan_runs WHERE id=?", (run["id"],))

        conn.execute("DELETE FROM roots WHERE id=?", (root_id,))
        # Work out which content-addressed cache files are truly exclusive only
        # after the root's file rows are gone.
        removable_hashes = {
            digest for digest in hashes
            if conn.execute("SELECT 1 FROM files WHERE sha256=? LIMIT 1", (digest,)).fetchone() is None
        }
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    cache = Path(cache_dir)
    removed_cache = 0
    # Unhashed thumbnails use a file/face id. Those ids are unique to the rows
    # just removed. Hashed keys can be shared across roots, hence the check above.
    patterns = (
        [(cache / "thumbs", f"fid{fid}_*") for fid in file_ids]
        + [(cache / "faces", f"fid{face_id}_*") for face_id in face_ids]
        + [(cache / "thumbs", f"{digest}_*") for digest in removable_hashes]
        + [(cache / "faces", f"{digest}_*") for digest in removable_hashes]
    )
    for directory, pattern in patterns:
        if not directory.is_dir():
            continue
        for item in directory.glob(pattern):
            try:
                if item.is_file():
                    item.unlink()
                    removed_cache += 1
            except OSError:
                # A cache miss or a concurrently-regenerated thumbnail must not
                # turn a completed database removal into an error.
                pass
    return {"ok": True, "path": root_path, "files": len(file_ids),
            "cache_files": removed_cache}


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
            where.append("f.id IN (SELECT fa.file_id FROM faces fa WHERE fa.person_id=?)")
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


def place_clusters(db_path: str, root_id: int) -> dict:
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
    return _read_place_clusters(db_path, root_id)


def _read_place_clusters(db_path: str, root_id: int) -> dict:
    conn = db.open_readonly(db_path)
    try:
        clusters = conn.execute(
            """SELECT id, name, lat, lon, member_count
               FROM place_clusters WHERE root_id=?
               ORDER BY CASE WHEN NULLIF(TRIM(name), '') IS NULL THEN 1 ELSE 0 END,
                        member_count DESC, id""", (root_id,)).fetchall()
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
        } for c in clusters]}
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


def place_cluster_members(db_path: str, cluster_id: int) -> dict | None:
    conn = db.open_readonly(db_path)
    try:
        c = conn.execute(
            "SELECT id, name, lat, lon FROM place_clusters WHERE id=?",
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
               ORDER BY (d.best_datetime IS NULL), d.best_datetime""",
            (cluster_id,)).fetchall()
        return {
            "id": c["id"], "name": c["name"], "lat": c["lat"], "lon": c["lon"],
            "members": [{
                "id": r["id"], "type": r["media_type"],
                "name": os.path.basename(r["rel_path"]),
                "date": r["dt"], "date_source": r["dsrc"],
                "has_gps": bool(r["has_gps"]),
            } for r in rows],
        }
    finally:
        conn.close()


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


# -- media grid + detail ----------------------------------------------------

def media(db_path: str, *, root_id=None, year=None, month=None, mtype=None,
          person_id=None, person_ids=None, cluster_id=None, limit=120, offset=0) -> dict:
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
        # every present file (~150k) and re-run the subquery per row — and for
        # the person case it picked idx_faces_person, pulling *all* of that
        # person's faces on each of the 150k iterations, which pushed the
        # request past 120s and left the grid stuck on a bare "Load more".
        selected_people = list(dict.fromkeys(
            person_ids or ([person_id] if person_id else [])))
        for selected_person in selected_people:
            where.append("f.id IN (SELECT fa.file_id FROM faces fa WHERE fa.person_id=?)")
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
                ORDER BY (d.best_datetime IS NULL), d.best_datetime DESC, f.id
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

    Only *named* people/places are offered — an unnamed auto-cluster is not a
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
                WHERE p.name IS NOT NULL AND {_NOT_HIDDEN}{rc}
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
                    cluster_id=None, min_similarity=-1.0, limit=120, offset=0,
                    alternate_vectors=None) -> dict:
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
            where.append("f.id IN (SELECT file_id FROM faces WHERE person_id=?)")
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
    """Present images not yet face-scanned (DB-only, no disk walk) — used by the
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


def pet_summary(db_path: str, root_id=None, model_source=None) -> dict:
    from ..pets import backend as pet_backend
    conn = db.open_readonly(db_path)
    try:
        rc, rp = _root_clause(root_id)
        total = conn.execute(
            f"""SELECT COUNT(*) FROM files f WHERE {_NOT_HIDDEN}
                AND f.media_type='image'{rc}""", rp).fetchone()[0]
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
        return {"pets": [{
            "id": row["pet_id"], "name": row["name"], "species": row["species"],
            "cover_detection_id": row["cover_detection_id"],
            "detections": row["detections"], "photos": row["photos"],
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
    conn = db.open_readonly(db_path)
    try:
        pet = conn.execute(
            "SELECT id,name,species FROM pets WHERE id=?", (pet_id,)).fetchone()
        if not pet:
            return None
        rc, rp = _root_clause(root_id)
        rows = conn.execute(
            f"""SELECT f.id,f.rel_path,d.best_datetime dt,a.id detection_id
                FROM animal_detections a JOIN files f ON f.id=a.file_id
                LEFT JOIN dates d ON d.file_id=f.id
                WHERE a.pet_id=? AND {_NOT_HIDDEN}{rc}
                ORDER BY (d.best_datetime IS NULL),d.best_datetime DESC,f.id
                LIMIT ? OFFSET ?""",
            (pet_id, *rp, limit, offset)).fetchall()
        total = conn.execute(
            f"""SELECT COUNT(DISTINCT a.file_id) FROM animal_detections a
                JOIN files f ON f.id=a.file_id
                WHERE a.pet_id=? AND {_NOT_HIDDEN}{rc}""",
            (pet_id, *rp)).fetchone()[0]
        return {
            "id": pet["id"], "name": pet["name"], "species": pet["species"],
            "photos": total,
            "items": [{"id": row["id"], "name": os.path.basename(row["rel_path"]),
                       "date": row["dt"], "detection_id": row["detection_id"],
                       "type": "image", "has_gps": False}
                      for row in rows],
            "offset": offset, "count": len(rows),
        }
    finally:
        conn.close()


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


def face_summary(db_path: str, root_id=None) -> dict:
    from ..faces import backend as fb
    conn = db.open_readonly(db_path)
    try:
        rc, rp = _root_clause(root_id)
        # Face detection only runs on canonical (non-duplicate) images, so every
        # count here is over _NOT_HIDDEN — the "total" is unique media, matching
        # the people grid and what actually gets scanned.
        total_images = conn.execute(
            f"SELECT COUNT(*) FROM files f WHERE {_NOT_HIDDEN} AND f.media_type='image'{rc}",
            rp).fetchone()[0]
        scanned = conn.execute(
            f"""SELECT COUNT(*) FROM files f JOIN face_scan s ON s.file_id=f.id
                WHERE {_NOT_HIDDEN} AND f.media_type='image'{rc}""", rp).fetchone()[0]
        faces = conn.execute(
            f"""SELECT COUNT(*) FROM faces fa JOIN files f ON f.id=fa.file_id
                WHERE {_NOT_HIDDEN} AND fa.not_person=0{rc}""", rp).fetchone()[0]
        people = conn.execute(
            f"""SELECT COUNT(DISTINCT fa.person_id) FROM faces fa
                JOIN files f ON f.id=fa.file_id
                WHERE {_NOT_HIDDEN} AND fa.person_id IS NOT NULL{rc}""", rp).fetchone()[0]
        photos_with_faces = conn.execute(
            f"""SELECT COUNT(DISTINCT fa.file_id) FROM faces fa
                JOIN files f ON f.id=fa.file_id
                WHERE {_NOT_HIDDEN} AND fa.not_person=0{rc}""", rp).fetchone()[0]
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
    """Up to k sharpest (highest det_score), non-hidden face ids per person — for
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
                WHERE fa.person_id IN ({marks}) AND f.hidden=0
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
                WHERE {_NOT_HIDDEN} AND fa.person_id IS NOT NULL{rc}
                GROUP BY fa.person_id
                ORDER BY CASE WHEN NULLIF(TRIM(p.name), '') IS NULL THEN 1 ELSE 0 END,
                         faces DESC, pid
                LIMIT ? OFFSET ?""", (*rp, limit, offset)).fetchall()
        prev = _preview_faces(conn, [r["pid"] for r in rows])
        people = [{
            "id": r["pid"], "name": r["name"], "cover_face_id": r["cover_face_id"],
            "faces_preview": prev.get(r["pid"], []),
            "photos": r["photos"], "faces": r["faces"],
        } for r in rows]
        return {"people": people, "offset": offset, "count": len(people)}
    finally:
        conn.close()


def face_person(db_path: str, person_id: int, root_id=None,
                limit=120, offset=0) -> dict | None:
    conn = db.open_readonly(db_path)
    try:
        p = conn.execute(
            "SELECT id, name FROM persons WHERE id=?", (person_id,)).fetchone()
        if not p:
            return None
        rc, rp = _root_clause(root_id)
        rows = conn.execute(
            f"""SELECT f.id, f.media_type, f.rel_path, d.best_datetime AS dt,
                       d.date_source AS dsrc,
                       EXISTS(SELECT 1 FROM geo g WHERE g.file_id=f.id) AS has_gps,
                       (SELECT fa2.id FROM faces fa2
                        WHERE fa2.file_id=f.id AND fa2.person_id=?
                        ORDER BY fa2.det_score DESC LIMIT 1) AS face_id
                FROM faces fa JOIN files f ON f.id=fa.file_id
                LEFT JOIN dates d ON d.file_id=f.id
                WHERE fa.person_id=? AND {_NOT_HIDDEN}{rc}
                GROUP BY f.id
                ORDER BY (d.best_datetime IS NULL), d.best_datetime DESC, f.id
                LIMIT ? OFFSET ?""",
            (person_id, person_id, *rp, limit, offset)).fetchall()
        total = conn.execute(
            f"""SELECT COUNT(DISTINCT fa.file_id) FROM faces fa
                JOIN files f ON f.id=fa.file_id
                WHERE fa.person_id=? AND {_NOT_HIDDEN}{rc}""",
            (person_id, *rp)).fetchone()[0]
        items = [{
            "id": r["id"], "type": r["media_type"],
            "name": os.path.basename(r["rel_path"]),
            "date": r["dt"], "date_source": r["dsrc"],
            "has_gps": bool(r["has_gps"]), "face_id": r["face_id"],
        } for r in rows]
        return {"id": person_id, "name": p["name"], "photos": total,
                "items": items, "offset": offset, "count": len(items)}
    finally:
        conn.close()


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
        "SELECT COUNT(*) FROM faces fa JOIN files f ON f.id=fa.file_id "
        "WHERE fa.person_id=? AND f.hidden=0", (pid,)).fetchone()[0]
    if left == 0 and not conn.execute(
            "SELECT 1 FROM faces WHERE person_id=?", (pid,)).fetchone():
        conn.execute("DELETE FROM persons WHERE id=?", (pid,))
        return
    cover = conn.execute(
        "SELECT fa.id FROM faces fa JOIN files f ON f.id=fa.file_id "
        "WHERE fa.person_id=? AND f.hidden=0 ORDER BY fa.det_score DESC LIMIT 1",
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


# -- "same person?" review: merges + constraints ---------------------------

def _rep_face(conn, pid, cover) -> int | None:
    """A stable representative face id for a person (its cover, or its sharpest
    face). Used to anchor a durable face_links constraint."""
    if cover:
        return cover
    r = conn.execute("SELECT id FROM faces WHERE person_id=? ORDER BY det_score DESC LIMIT 1",
                     (pid,)).fetchone()
    return r["id"] if r else None


def _record_link(conn, fa, fb, kind: str, now: str) -> None:
    if not fa or not fb or fa == fb:
        return
    a, b = sorted((fa, fb))
    conn.execute("INSERT OR REPLACE INTO face_links(face_a, face_b, kind, created_at) "
                 "VALUES(?,?,?,?)", (a, b, kind, now))


def _update_person_centroid(conn, pid) -> None:
    import numpy as np
    rows = conn.execute(
        "SELECT fa.embedding e FROM faces fa JOIN files f ON f.id=fa.file_id "
        "WHERE fa.person_id=? AND f.hidden=0", (pid,)).fetchall()
    if not rows:
        return
    X = np.array([np.frombuffer(r["e"], "float32") for r in rows], dtype="float32")
    X /= np.linalg.norm(X, axis=1, keepdims=True) + 1e-9
    c = X.mean(0)
    c = (c / (np.linalg.norm(c) + 1e-9)).astype("float32")
    conn.execute("UPDATE persons SET centroid=? WHERE id=?", (c.tobytes(), pid))


def merge_persons(db_path: str, id_a, id_b) -> dict:
    """User confirmed two clusters are the same person. Merge immediately (move
    faces, keep the named/larger one) AND store a durable 'same' constraint so
    the merge survives future re-clusters."""
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
        if pa["name"] and pb["name"] and pa["name"] != pb["name"]:
            return {"error": f"both named ({pa['name']} / {pb['name']}); rename first"}
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
        _record_link(conn, _rep_face(conn, keep["id"], keep["cover_face_id"]),
                     _rep_face(conn, drop["id"], drop["cover_face_id"]), "same", now)
        conn.execute("UPDATE faces SET person_id=? WHERE person_id=?", (keep["id"], drop["id"]))
        conn.execute("DELETE FROM persons WHERE id=?", (drop["id"],))
        _sync_person_stats(conn, keep["id"])
        _update_person_centroid(conn, keep["id"])
        conn.commit()
        r = conn.execute("SELECT id,name,face_count FROM persons WHERE id=?", (keep["id"],)).fetchone()
        return {"ok": True, "person": {"id": r["id"], "name": r["name"], "face_count": r["face_count"]}}
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
    'different'. This is the review queue — the pairs the automatic pass left
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
        # 'skip' (reviewed, undecided) — both are removed from the queue so it
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
    """Attach a file to a place as a MANUAL member (membership only — no geo is
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
    """(abs path, sha256, box) for a face id, or None. Path is DB-derived."""
    conn = db.open_readonly(db_path)
    try:
        r = conn.execute(
            """SELECT r.path AS root, f.rel_path, f.sha256,
                      fa.box_x, fa.box_y, fa.box_w, fa.box_h
               FROM faces fa JOIN files f ON f.id=fa.file_id
               JOIN roots r ON r.id=f.root_id WHERE fa.id=?""", (face_id,)).fetchone()
        if not r:
            return None
        p = Path(r["root"]) / r["rel_path"]
        if not p.is_file():
            return None
        return p, r["sha256"], (r["box_x"], r["box_y"], r["box_w"], r["box_h"])
    finally:
        conn.close()


def animal_crop_source(db_path: str, detection_id: int):
    """(abs path, sha256, box) for an animal detection id."""
    conn = db.open_readonly(db_path)
    try:
        row = conn.execute(
            """SELECT r.path root,f.rel_path,f.sha256,
                      a.box_x,a.box_y,a.box_w,a.box_h
               FROM animal_detections a JOIN files f ON f.id=a.file_id
               JOIN roots r ON r.id=f.root_id WHERE a.id=?""",
            (detection_id,)).fetchone()
        if not row:
            return None
        path = Path(row["root"]) / row["rel_path"]
        if not path.is_file():
            return None
        return path, row["sha256"], (
            row["box_x"], row["box_y"], row["box_w"], row["box_h"])
    finally:
        conn.close()


def item(db_path: str, fid: int) -> dict | None:
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
            """SELECT fa.id AS face_id, fa.person_id, p.name
               FROM faces fa LEFT JOIN persons p ON p.id=fa.person_id
               WHERE fa.file_id=? AND fa.not_person=0
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
        place = conn.execute(
            """SELECT pc.id, pc.name FROM place_cluster_members pcm
               JOIN place_clusters pc ON pc.id=pcm.cluster_id
               WHERE pcm.file_id=? LIMIT 1""", (fid,)).fetchone()
        # Pick-lists for in-panel editing: only *named* places (in this file's root)
        # and *named* persons are offered as targets.
        place_options = [{"id": r["id"], "name": r["name"]} for r in conn.execute(
            """SELECT id, name FROM place_clusters
               WHERE root_id=? AND name IS NOT NULL
               ORDER BY name COLLATE NOCASE""", (f["root_id"],))]
        person_options = [{"id": r["id"], "name": r["name"]} for r in conn.execute(
            """SELECT id, name FROM persons WHERE name IS NOT NULL AND name != ''
               ORDER BY name COLLATE NOCASE""")]
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
        }
    finally:
        conn.close()


def file_location(db_path: str, fid: int) -> Path | None:
    """Absolute path of a file id, or None. Path comes from the DB (never from
    the request), so the server cannot be tricked into path traversal."""
    conn = db.open_readonly(db_path)
    try:
        r = conn.execute(
            """SELECT r.path AS root, f.rel_path FROM files f
               JOIN roots r ON r.id=f.root_id WHERE f.id=?""", (fid,)).fetchone()
        if not r:
            return None
        p = Path(r["root"]) / r["rel_path"]
        return p if p.is_file() else None
    finally:
        conn.close()


def thumb_source(db_path: str, fid: int) -> tuple[Path, str | None] | None:
    """(absolute path, content sha256) for a file id, or None if missing.

    The sha256 is what the thumbnail cache is keyed on, so byte-identical
    duplicates (rife in this cross-takeout archive) share one thumbnail and can
    never disagree. Path is DB-derived, so no request-driven path traversal."""
    conn = db.open_readonly(db_path)
    try:
        r = conn.execute(
            """SELECT r.path AS root, f.rel_path, f.sha256 FROM files f
               JOIN roots r ON r.id=f.root_id WHERE f.id=?""", (fid,)).fetchone()
        if not r:
            return None
        p = Path(r["root"]) / r["rel_path"]
        if not p.is_file():
            return None
        return p, r["sha256"]
    finally:
        conn.close()
