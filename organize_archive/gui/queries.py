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
        # Un-face-scanned images (only relevant when the face backend can run).
        from ..faces import backend as fb
        not_faced = 0
        if fb.available():
            images = conn.execute(
                f"""SELECT COUNT(*) FROM files f
                    WHERE {_VISIBLE} AND f.media_type='image' AND f.root_id=?""",
                (root_id,)).fetchone()[0]
            faced = conn.execute(
                f"""SELECT COUNT(*) FROM files f JOIN face_scan s ON s.file_id=f.id
                    WHERE {_VISIBLE} AND f.media_type='image' AND f.root_id=?""",
                (root_id,)).fetchone()[0]
            not_faced = max(0, images - faced)
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
        "not_faced": not_faced,
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


# -- faces / people ---------------------------------------------------------

def faces_pending(db_path: str, root_id=None) -> int:
    """Present images not yet face-scanned (DB-only, no disk walk) — used by the
    auto-scheduler to decide whether to queue a faces job."""
    conn = db.open_readonly(db_path)
    try:
        rc, rp = _root_clause(root_id)
        return conn.execute(
            f"""SELECT COUNT(*) FROM files f
                LEFT JOIN face_scan s ON s.file_id=f.id
                WHERE s.file_id IS NULL AND {_VISIBLE} AND f.media_type='image'{rc}""",
            rp).fetchone()[0]
    finally:
        conn.close()


def face_summary(db_path: str, root_id=None) -> dict:
    from ..faces import backend as fb
    conn = db.open_readonly(db_path)
    try:
        rc, rp = _root_clause(root_id)
        total_images = conn.execute(
            f"SELECT COUNT(*) FROM files f WHERE {_VISIBLE} AND f.media_type='image'{rc}",
            rp).fetchone()[0]
        scanned = conn.execute(
            f"""SELECT COUNT(*) FROM files f JOIN face_scan s ON s.file_id=f.id
                WHERE {_VISIBLE} AND f.media_type='image'{rc}""", rp).fetchone()[0]
        faces = conn.execute(
            f"""SELECT COUNT(*) FROM faces fa JOIN files f ON f.id=fa.file_id
                WHERE {_VISIBLE}{rc}""", rp).fetchone()[0]
        people = conn.execute(
            f"""SELECT COUNT(DISTINCT fa.person_id) FROM faces fa
                JOIN files f ON f.id=fa.file_id
                WHERE {_VISIBLE} AND fa.person_id IS NOT NULL{rc}""", rp).fetchone()[0]
        photos_with_faces = conn.execute(
            f"""SELECT COUNT(DISTINCT fa.file_id) FROM faces fa
                JOIN files f ON f.id=fa.file_id WHERE {_VISIBLE}{rc}""", rp).fetchone()[0]
        return {
            "total_images": total_images, "scanned": scanned,
            "unscanned": max(0, total_images - scanned),
            "faces": faces, "people": people,
            "photos_with_faces": photos_with_faces,
            "backend_available": fb.available(),
        }
    finally:
        conn.close()


def face_persons(db_path: str, root_id=None, limit=120, offset=0) -> dict:
    """People (clusters) that appear in this archive, most faces first. Each
    carries a cover face for the card and its photo/face counts here."""
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
                ORDER BY faces DESC, pid
                LIMIT ? OFFSET ?""", (*rp, limit, offset)).fetchall()
        people = [{
            "id": r["pid"], "name": r["name"], "cover_face_id": r["cover_face_id"],
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
        cur = conn.execute(
            "UPDATE persons SET name=? WHERE id=?", (name or None, person_id))
        conn.commit()
        if cur.rowcount == 0:
            return {"error": "not found"}
        return {"ok": True, "name": name or None}
    finally:
        conn.close()


def recompute_people(db_path: str, cfg) -> dict:
    """Re-run clustering over all current face embeddings (idempotent)."""
    from ..faces.cluster import cluster_faces
    conn = db.connect(db_path)
    try:
        stats = cluster_faces(conn, cfg)
        return {"people": stats.people, "clustered": stats.clustered,
                "noise": stats.noise, "named": stats.named}
    finally:
        conn.close()


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
               WHERE fa.file_id=? ORDER BY fa.det_score DESC""", (fid,))]
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
            "people": people,
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
