"""Enrichment pipeline: resolve dates, GPS and media metadata for indexed files.

Reads each file's Takeout sidecar, EXIF (via exiftool), and filename, then
writes media_meta / dates / geo / takeout_sidecar rows. Resumable: only files
without a resolved date are processed, in batches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from ..db import database as db
from . import filename_dates, resolver
from .takeout import SidecarMatcher, parse_sidecar

# exiftool is optional; without it we still resolve from Takeout/filename/mtime.
try:
    from .exiftool_reader import ExifReader, available as exif_available
except Exception:  # pragma: no cover
    ExifReader = None

    def exif_available() -> bool:
        return False


@dataclass
class EnrichStats:
    processed: int = 0
    with_takeout: int = 0
    with_gps: int = 0
    unmatched_takeout: int = 0
    by_source: dict = field(default_factory=dict)


_MIME_TO_TYPE = {"image": "image", "video": "video", "audio": "audio"}


def _detected_type(mime: str | None) -> str | None:
    if not mime:
        return None
    top = mime.split("/", 1)[0].lower()
    if top in _MIME_TO_TYPE:
        return _MIME_TO_TYPE[top]
    if mime.lower() == "application/pdf":
        return "document"
    return None


def _gps_from_exif(tags: dict):
    lat = tags.get("GPSLatitude")
    lon = tags.get("GPSLongitude")
    if lat is None or lon is None:
        return None
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return None
    if not lat and not lon:
        return None
    if str(tags.get("GPSLatitudeRef", "")).upper().startswith("S"):
        lat = -abs(lat)
    if str(tags.get("GPSLongitudeRef", "")).upper().startswith("W"):
        lon = -abs(lon)
    alt = tags.get("GPSAltitude")
    try:
        alt = float(alt) if alt is not None else None
    except (TypeError, ValueError):
        alt = None
    return lat, lon, alt


def _pending(conn, batch_size: int):
    return conn.execute(
        """SELECT f.id, f.rel_path, f.media_type, f.mtime, r.path AS root_path
           FROM files f JOIN roots r ON r.id = f.root_id
           LEFT JOIN dates d ON d.file_id = f.id
           WHERE d.file_id IS NULL AND f.present = 1
           ORDER BY f.id
           LIMIT ?""",
        (batch_size,),
    ).fetchall()


def enrich(conn, cfg: Config, progress=None, batch_size: int = 300) -> EnrichStats:
    stats = EnrichStats()
    matcher = SidecarMatcher()
    reader = ExifReader() if (ExifReader and exif_available()) else None

    total = conn.execute(
        """SELECT COUNT(*) FROM files f LEFT JOIN dates d ON d.file_id=f.id
           WHERE d.file_id IS NULL AND f.present=1"""
    ).fetchone()[0]
    if progress is not None:
        progress.total = total

    while True:
        rows = _pending(conn, batch_size)
        if not rows:
            break

        abs_paths = {r["id"]: Path(r["root_path"]) / r["rel_path"] for r in rows}
        mtype = {r["id"]: r["media_type"] for r in rows}

        exif_map: dict[str, dict] = {}
        if reader is not None:
            to_read = [p for fid, p in abs_paths.items() if mtype[fid] != "archive"]
            exif_map = reader.read_batch(to_read)

        for row in rows:
            fid = row["id"]
            path = abs_paths[fid]
            name = path.name
            tags = exif_map.get(str(path), {})
            candidates: dict = {}

            # --- Takeout sidecar ---
            side = None
            match = matcher.find(path)
            if match is not None:
                json_path, method, mconf = match
                side = parse_sidecar(json_path)
                if side is not None:
                    stats.with_takeout += 1
                    conn.execute(
                        """INSERT OR REPLACE INTO takeout_sidecar
                           (file_id, json_rel_path, title, description,
                            taken_time, match_method, match_confidence)
                           VALUES (?,?,?,?,?,?,?)""",
                        (fid, json_path.name, side.title, side.description,
                         side.taken_time, method, mconf),
                    )
                    if side.taken_time:
                        dt = resolver.epoch_to_wall(side.taken_time, cfg.timezone)
                        candidates["takeout_json"] = (dt, min(0.97, 0.6 + mconf * 0.37))

            # --- EXIF ---
            ex = resolver.exif_datetime(tags)
            if ex is not None:
                candidates["exif"] = ex

            # --- Filename ---
            fn = filename_dates.parse(name)
            if fn is not None:
                candidates["filename"] = fn

            # --- mtime (always available) ---
            candidates["mtime"] = (
                resolver.epoch_to_wall(int(row["mtime"]), cfg.timezone), 0.2
            )

            resolved = resolver.resolve(candidates, cfg.date_priority)
            if resolved is not None:
                dt, source, conf = resolved
                conn.execute(
                    """INSERT OR REPLACE INTO dates
                       (file_id, best_datetime, date_source, date_confidence)
                       VALUES (?,?,?,?)""",
                    (fid, resolver.to_iso(dt), source, conf),
                )
                stats.by_source[source] = stats.by_source.get(source, 0) + 1

            # --- GPS ---
            geo = None
            geo_source = None
            if side is not None and side.lat is not None:
                geo = (side.lat, side.lon, side.alt)
                geo_source = "takeout_json"
            else:
                eg = _gps_from_exif(tags)
                if eg is not None:
                    geo = eg
                    geo_source = "exif"
            if geo is not None:
                conn.execute(
                    """INSERT OR REPLACE INTO geo (file_id, lat, lon, alt, geo_source)
                       VALUES (?,?,?,?,?)""",
                    (fid, geo[0], geo[1], geo[2], geo_source),
                )
                stats.with_gps += 1

            # --- media_meta + content-type fix ---
            mime = tags.get("MIMEType")
            det = _detected_type(mime)
            conn.execute(
                """INSERT OR REPLACE INTO media_meta
                   (file_id, width, height, duration_s, make, model,
                    orientation, mime, detected_type)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (fid,
                 _num(tags.get("ImageWidth")), _num(tags.get("ImageHeight")),
                 _num(tags.get("Duration")), tags.get("Make"), tags.get("Model"),
                 _num(tags.get("Orientation")), mime, det),
            )
            if row["media_type"] == "other" and det:
                conn.execute("UPDATE files SET media_type=? WHERE id=?", (det, fid))

            stats.processed += 1
            if progress is not None:
                progress.update(stats.processed, 0)

        conn.commit()

    return stats


def _num(v):
    if v is None:
        return None
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return None
