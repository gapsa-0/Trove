"""Enrichment pipeline: resolve dates, GPS and media metadata for indexed files.

Reads each file's Takeout sidecar, EXIF (via exiftool), and filename, then
writes media_meta / dates / geo / takeout_sidecar rows. Resumable: only files
without a resolved date are processed, in batches.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import Config
from ..progress import Progress
from . import filename_dates, resolver
from .takeout import SidecarData, SidecarMatcher, parse_sidecar

logger = logging.getLogger(__name__)

# exiftool is optional; without it we still resolve from Takeout/filename/mtime.
# Pre-declared so both the success and fallback branches below bind the same
# name at the same (Optional) type -- a plain `ExifReader = None` in the except
# branch would otherwise conflict with the class bound to it in the try branch.
ExifReader: type[Any] | None

try:
    from .exiftool_reader import ExifReader
    from .exiftool_reader import available as exif_available
except Exception:  # pragma: no cover
    # Broad on purpose: this wraps a native-tool binding (pyexiftool), which can
    # fail in more ways than ImportError when exiftool itself is missing or
    # broken. Running without exiftool is a supported configuration, so DEBUG.
    logger.debug("exiftool reader unavailable; enrich will run without EXIF", exc_info=True)
    ExifReader = None

    def exif_available() -> bool:
        """Always False: this stub only exists because the real exiftool binding failed to import."""
        return False


@dataclass
class EnrichStats:
    """Counters for one ``enrich`` run, returned to the caller when the batch loop ends."""

    processed: int = 0
    with_takeout: int = 0
    with_gps: int = 0
    unmatched_takeout: int = 0
    by_source: dict[str, int] = field(default_factory=dict)


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


def _gps_from_exif(tags: dict[str, Any]) -> tuple[float, float, float | None] | None:
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


def _pending(
    conn: sqlite3.Connection, batch_size: int, root_ids: tuple[int, ...] | None = None
) -> list[sqlite3.Row]:
    """Return an enrichment batch, optionally restricted to scan roots."""
    where = "d.file_id IS NULL AND f.present = 1"
    params: list[int] = []
    if root_ids:
        where += " AND f.root_id IN (" + ",".join("?" for _ in root_ids) + ")"
        params.extend(root_ids)
    params.append(batch_size)
    return conn.execute(
        f"""SELECT f.id, f.rel_path, f.media_type, f.mtime, r.path AS root_path
            FROM files f JOIN roots r ON r.id = f.root_id
            LEFT JOIN dates d ON d.file_id = f.id
            WHERE {where}
            ORDER BY f.id
            LIMIT ?""",
        params,
    ).fetchall()


def _count_pending(conn: sqlite3.Connection, root_ids: tuple[int, ...] | None) -> int:
    """How many files still lack a ``dates`` row, for the progress total."""
    where = "d.file_id IS NULL AND f.present=1"
    params: list[int] = []
    if root_ids:
        where += " AND f.root_id IN (" + ",".join("?" for _ in root_ids) + ")"
        params.extend(root_ids)
    n: int = conn.execute(
        f"""SELECT COUNT(*) FROM files f LEFT JOIN dates d ON d.file_id=f.id
            WHERE {where}""",
        params,
    ).fetchone()[0]
    return n


def _store_sidecar(
    conn: sqlite3.Connection,
    fid: int,
    path: Path,
    matcher: SidecarMatcher,
    stats: EnrichStats,
) -> tuple[SidecarData | None, float]:
    """Find and record this file's Takeout sidecar, if it has one.

    Returns ``(sidecar, match_confidence)``, or ``(None, 0.0)``. The confidence
    is the *matcher's* -- how sure we are this JSON belongs to this file -- and
    feeds the date confidence below, which is why it comes back out rather than
    being folded in here.
    """
    match = matcher.find(path)
    if match is None:
        return None, 0.0
    json_path, method, mconf = match
    side = parse_sidecar(json_path)
    if side is None:
        return None, 0.0
    stats.with_takeout += 1
    conn.execute(
        """INSERT OR REPLACE INTO takeout_sidecar
           (file_id, json_rel_path, title, description,
            taken_time, match_method, match_confidence)
           VALUES (?,?,?,?,?,?,?)""",
        (
            fid,
            json_path.name,
            side.title,
            side.description,
            side.taken_time,
            method,
            mconf,
        ),
    )
    return side, mconf


def _date_candidates(
    row: sqlite3.Row,
    name: str,
    tags: dict[str, Any],
    side: SidecarData | None,
    mconf: float,
    cfg: Config,
) -> dict[str, tuple[datetime, float]]:
    """Every date this file offers, keyed by source, for the resolver to rank.

    Every source that has an opinion contributes; ``cfg.date_priority`` decides
    between them, not the order here. mtime is always present, so there is always
    something to fall back to.
    """
    candidates: dict[str, tuple[datetime, float]] = {}
    if side is not None and side.taken_time:
        dt = resolver.epoch_to_wall(side.taken_time, cfg.timezone)
        candidates["takeout_json"] = (dt, min(0.97, 0.6 + mconf * 0.37))
    ex = resolver.exif_datetime(tags)
    if ex is not None:
        candidates["exif"] = ex
    fn = filename_dates.parse(name, day_first=cfg.filename_date_day_first)
    if fn is not None:
        candidates["filename"] = fn
    candidates["mtime"] = (resolver.epoch_to_wall(int(row["mtime"]), cfg.timezone), 0.2)
    return candidates


def _store_date(
    conn: sqlite3.Connection,
    fid: int,
    candidates: dict[str, tuple[datetime, float]],
    cfg: Config,
    stats: EnrichStats,
) -> None:
    """Write the winning date, or nothing at all.

    A file with no resolvable date is deliberately left without a ``dates`` row
    rather than given a placeholder, so the next run picks it up again.
    """
    resolved = resolver.resolve(candidates, cfg.date_priority)
    if resolved is None:
        return
    dt, source, conf = resolved
    conn.execute(
        """INSERT OR REPLACE INTO dates
           (file_id, best_datetime, date_source, date_confidence)
           VALUES (?,?,?,?)""",
        (fid, resolver.to_iso(dt), source, conf),
    )
    stats.by_source[source] = stats.by_source.get(source, 0) + 1


def _store_gps(
    conn: sqlite3.Connection,
    fid: int,
    tags: dict[str, Any],
    side: SidecarData | None,
    stats: EnrichStats,
) -> None:
    """Write a coordinate from the sidecar, else from EXIF, else nothing."""
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
    if geo is None:
        return
    conn.execute(
        """INSERT OR REPLACE INTO geo (file_id, lat, lon, alt, geo_source)
           VALUES (?,?,?,?,?)""",
        (fid, geo[0], geo[1], geo[2], geo_source),
    )
    stats.with_gps += 1


def _store_media_meta(
    conn: sqlite3.Connection, fid: int, row: sqlite3.Row, tags: dict[str, Any]
) -> None:
    """Write dimensions/duration/camera, and correct the file's type if the
    scanner had to guess. A file the extension said nothing about ('other')
    takes the MIME type's word for it."""
    mime = tags.get("MIMEType")
    det = _detected_type(mime)
    conn.execute(
        """INSERT OR REPLACE INTO media_meta
           (file_id, width, height, duration_s, make, model,
            orientation, mime, detected_type)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            fid,
            _num(tags.get("ImageWidth")),
            _num(tags.get("ImageHeight")),
            _num(tags.get("Duration")),
            tags.get("Make"),
            tags.get("Model"),
            _num(tags.get("Orientation")),
            mime,
            det,
        ),
    )
    if row["media_type"] == "other" and det:
        conn.execute("UPDATE files SET media_type=? WHERE id=?", (det, fid))


def _enrich_one(
    conn: sqlite3.Connection,
    cfg: Config,
    row: sqlite3.Row,
    path: Path,
    tags: dict[str, Any],
    matcher: SidecarMatcher,
    stats: EnrichStats,
) -> None:
    """One file's four derived facts. The sidecar is read first because both the
    date and the GPS defer to it where it has an opinion."""
    fid = row["id"]
    side, mconf = _store_sidecar(conn, fid, path, matcher, stats)
    _store_date(conn, fid, _date_candidates(row, path.name, tags, side, mconf, cfg), cfg, stats)
    _store_gps(conn, fid, tags, side, stats)
    _store_media_meta(conn, fid, row, tags)
    stats.processed += 1


def enrich(
    conn: sqlite3.Connection,
    cfg: Config,
    progress: Progress | None = None,
    batch_size: int = 80,
    root_ids: tuple[int, ...] | None = None,
) -> EnrichStats:
    """Resolve dates/GPS/media metadata for every file still missing a ``dates`` row.

    Processes in batches, committing periodically, until no pending files
    remain, then returns the accumulated ``EnrichStats``. A file with no
    resolvable date is simply left without a ``dates`` row and picked up
    again on the next call.
    """
    stats = EnrichStats()
    matcher = SidecarMatcher()
    reader = ExifReader() if (ExifReader and exif_available()) else None
    import time as _time

    _last_commit = _time.monotonic()

    total = _count_pending(conn, root_ids)
    if progress is not None:
        progress.total = total

    while True:
        rows = _pending(conn, batch_size, root_ids)
        if not rows:
            break

        abs_paths = {r["id"]: Path(r["root_path"]) / r["rel_path"] for r in rows}
        mtype = {r["id"]: r["media_type"] for r in rows}

        # One exiftool invocation per batch, not per file: spawning the process
        # is most of the cost, so reading 80 files at once is what makes this
        # stage tolerable over ~150k files.
        exif_map: dict[str, dict[str, Any]] = {}
        if reader is not None:
            to_read = [p for fid, p in abs_paths.items() if mtype[fid] != "archive"]
            exif_map = reader.read_batch(to_read)

        for row in rows:
            path = abs_paths[row["id"]]
            _enrich_one(conn, cfg, row, path, exif_map.get(str(path), {}), matcher, stats)
            if progress is not None:
                progress.update(stats.processed, 0)

            # Flush to DB by time so the API reflects near-real-time counts.
            if (_time.monotonic() - _last_commit) >= 2:
                conn.commit()
                _last_commit = _time.monotonic()

        conn.commit()
        _last_commit = _time.monotonic()

    return stats


def _num(v: Any) -> int | float | None:
    if v is None:
        return None
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return None
