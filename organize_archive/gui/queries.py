"""Read-only queries backing the GUI. Each opens its own connection so the
handler stays thread-safe under ThreadingHTTPServer.

Everything is scoped to an archive (a row in `roots`). A root_id of None means
"all archives combined".
"""

from __future__ import annotations

import os
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


def freshness(db_path: str, root_id: int) -> dict:
    """Compare files on disk to what's indexed/enriched, so the UI can show a
    real status instead of an always-on Scan button. The disk count is a walk
    (scandir only, no hashing)."""
    from ..scan.walker import count_files
    conn = db.open_readonly(db_path)
    try:
        r = conn.execute("SELECT path FROM roots WHERE id=?", (root_id,)).fetchone()
        if not r:
            return {"error": "unknown archive"}
        indexed = conn.execute(
            f"SELECT COUNT(*) FROM files f WHERE {_VISIBLE} AND f.root_id=?",
            (root_id,)).fetchone()[0]
        enriched = conn.execute(
            f"""SELECT COUNT(*) FROM files f JOIN dates d ON d.file_id=f.id
                WHERE {_VISIBLE} AND f.root_id=?""", (root_id,)).fetchone()[0]
    finally:
        conn.close()
    p = Path(r["path"])
    if not p.is_dir():
        return {"exists": False, "indexed": indexed, "enriched": enriched}
    on_disk = count_files(p)
    return {
        "exists": True,
        "on_disk": on_disk,
        "indexed": indexed,
        "enriched": enriched,
        "new_files": max(0, on_disk - indexed),
        "not_enriched": max(0, indexed - enriched),
    }


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


def timeline(db_path: str, root_id=None, bucket="month") -> dict:
    """Frequency of media over time. bucket: 'month' or 'year'."""
    conn = db.open_readonly(db_path)
    try:
        rc, rp = _root_clause(root_id)
        span = 7 if bucket == "month" else 4      # 'YYYY-MM' vs 'YYYY'
        rows = conn.execute(
            f"""SELECT substr(d.best_datetime,1,{span}) period,
                       f.media_type mt, COUNT(*) c
                FROM files f JOIN dates d ON d.file_id=f.id
                WHERE {_VISIBLE}{rc} AND d.best_datetime IS NOT NULL
                GROUP BY period, mt ORDER BY period""", rp).fetchall()
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
               ORDER BY member_count DESC""", (root_id,)).fetchall()
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
          include_dups=False, limit=120, offset=0) -> dict:
    conn = db.open_readonly(db_path)
    try:
        where = [_VISIBLE if include_dups else _NOT_HIDDEN]
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
        clause = " AND ".join(where)
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
        return {"items": items, "offset": offset, "limit": limit, "count": len(items)}
    finally:
        conn.close()


def dup_summary(db_path: str, root_id=None) -> dict:
    conn = db.open_readonly(db_path)
    try:
        rc, rp = _root_clause(root_id)
        # groups whose canonical is in this archive (a group can span archives)
        row = conn.execute(
            f"""SELECT COUNT(*) groups,
                       COALESCE(SUM(g.member_count-1),0) dups,
                       COALESCE(SUM(g.redundant_bytes),0) bytes
                FROM dup_groups g
                JOIN files f ON f.id=g.canonical_file_id
                WHERE g.method='exact'{rc}""", rp).fetchone()
        return {"groups": row["groups"], "duplicates": row["dups"],
                "reclaimable": row["bytes"]}
    finally:
        conn.close()


def dup_groups(db_path: str, root_id=None, limit=60, offset=0) -> dict:
    conn = db.open_readonly(db_path)
    try:
        rc, rp = _root_clause(root_id)
        groups = conn.execute(
            f"""SELECT g.id, g.member_count, g.size_each, g.redundant_bytes,
                       g.canonical_file_id
                FROM dup_groups g JOIN files f ON f.id=g.canonical_file_id
                WHERE g.method='exact'{rc}
                ORDER BY g.redundant_bytes DESC, g.id
                LIMIT ? OFFSET ?""", (*rp, limit, offset)).fetchall()
        out = []
        for g in groups:
            members = conn.execute(
                """SELECT f.id, f.media_type, f.rel_path, m.role,
                          r.path AS root
                   FROM dup_members m JOIN files f ON f.id=m.file_id
                   JOIN roots r ON r.id=f.root_id
                   WHERE m.group_id=? ORDER BY (m.role='duplicate'), f.id""",
                (g["id"],)).fetchall()
            out.append({
                "id": g["id"], "count": g["member_count"],
                "size_each": g["size_each"], "reclaimable": g["redundant_bytes"],
                "canonical_id": g["canonical_file_id"],
                "members": [{
                    "id": m["id"], "type": m["media_type"], "role": m["role"],
                    "name": os.path.basename(m["rel_path"]),
                    "folder": os.path.dirname(m["rel_path"]),
                } for m in members],
            })
        return {"groups": out, "offset": offset, "count": len(out)}
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
        return {
            "id": fid, "name": os.path.basename(f["rel_path"]),
            "rel_path": f["rel_path"], "type": f["media_type"], "size": f["size"],
            "date": d["best_datetime"] if d else None,
            "date_source": d["date_source"] if d else None,
            "date_confidence": d["date_confidence"] if d else None,
            "gps": ({"lat": g["lat"], "lon": g["lon"], "alt": g["alt"],
                     "source": g["geo_source"]} if g else None),
            "meta": (dict(m) if m else None),
            "description": (t["description"] if t else None),
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
