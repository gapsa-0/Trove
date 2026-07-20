"""Read-only queries backing the GUI. Each opens its own connection so the
handler stays thread-safe under ThreadingHTTPServer.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..db import database as db

_VISIBLE = "f.present = 1 AND f.hidden = 0"


def summary(db_path: str) -> dict:
    conn = db.open_readonly(db_path)
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM files f WHERE {_VISIBLE}"
        ).fetchone()[0]
        years = [
            {"year": r["y"], "count": r["c"]}
            for r in conn.execute(
                f"""SELECT substr(d.best_datetime,1,4) y, COUNT(*) c
                    FROM files f JOIN dates d ON d.file_id=f.id
                    WHERE {_VISIBLE} AND d.best_datetime IS NOT NULL
                    GROUP BY y ORDER BY y DESC"""
            )
        ]
        types = [
            {"type": r["media_type"], "count": r["c"]}
            for r in conn.execute(
                f"""SELECT media_type, COUNT(*) c FROM files f
                    WHERE {_VISIBLE} GROUP BY media_type ORDER BY c DESC"""
            )
        ]
        gps = conn.execute(
            f"""SELECT COUNT(*) FROM files f JOIN geo g ON g.file_id=f.id
                WHERE {_VISIBLE}"""
        ).fetchone()[0]
        return {"total": total, "years": years, "types": types, "with_gps": gps}
    finally:
        conn.close()


def media(db_path: str, *, year=None, month=None, mtype=None,
          limit=120, offset=0) -> dict:
    conn = db.open_readonly(db_path)
    try:
        where = [_VISIBLE]
        params: list = []
        if mtype:
            where.append("f.media_type = ?")
            params.append(mtype)
        if year:
            where.append("substr(d.best_datetime,1,4) = ?")
            params.append(str(year))
        if month:  # 'YYYY-MM'
            where.append("substr(d.best_datetime,1,7) = ?")
            params.append(month)
        clause = " AND ".join(where)

        rows = conn.execute(
            f"""SELECT f.id, f.media_type, f.rel_path, d.best_datetime AS dt,
                       d.date_source AS dsrc,
                       EXISTS(SELECT 1 FROM geo g WHERE g.file_id=f.id) AS has_gps
                FROM files f LEFT JOIN dates d ON d.file_id=f.id
                WHERE {clause}
                ORDER BY (d.best_datetime IS NULL), d.best_datetime DESC, f.id
                LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()
        items = [
            {
                "id": r["id"], "type": r["media_type"],
                "name": os.path.basename(r["rel_path"]),
                "date": r["dt"], "date_source": r["dsrc"],
                "has_gps": bool(r["has_gps"]),
            }
            for r in rows
        ]
        return {"items": items, "offset": offset, "limit": limit,
                "count": len(items)}
    finally:
        conn.close()


def item(db_path: str, fid: int) -> dict | None:
    conn = db.open_readonly(db_path)
    try:
        f = conn.execute(
            """SELECT f.*, r.path AS root_path FROM files f
               JOIN roots r ON r.id=f.root_id WHERE f.id=?""", (fid,)
        ).fetchone()
        if not f:
            return None
        d = conn.execute("SELECT * FROM dates WHERE file_id=?", (fid,)).fetchone()
        g = conn.execute("SELECT * FROM geo WHERE file_id=?", (fid,)).fetchone()
        m = conn.execute("SELECT * FROM media_meta WHERE file_id=?", (fid,)).fetchone()
        t = conn.execute(
            "SELECT * FROM takeout_sidecar WHERE file_id=?", (fid,)
        ).fetchone()
        return {
            "id": fid,
            "name": os.path.basename(f["rel_path"]),
            "rel_path": f["rel_path"],
            "type": f["media_type"],
            "size": f["size"],
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
               JOIN roots r ON r.id=f.root_id WHERE f.id=?""", (fid,)
        ).fetchone()
        if not r:
            return None
        p = Path(r["root"]) / r["rel_path"]
        return p if p.is_file() else None
    finally:
        conn.close()
