"""Resumable animal extraction and face/non-human reconciliation."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from ..db import database as db
from ..progress import Progress
from . import backend
from .backend import Log, PetBackend

logger = logging.getLogger(__name__)


@dataclass
class PetExtractStats:
    processed: int = 0
    animals: int = 0
    photos_with_animals: int = 0
    faces_suppressed: int = 0
    errors: int = 0
    error_samples: list[str] = field(default_factory=list)


def scan_source(cfg: Config) -> str:
    """Stable provenance/cache key for every setting that changes detections."""
    species = ",".join(sorted(set(cfg.pets_species)))
    return (
        f"{cfg.pets_model_version};score={cfg.pets_min_score:g};"
        f"minpx={cfg.pets_min_px};side={cfg.pets_max_side};species={species};"
        f"human={cfg.pets_human_min_score:g}/{cfg.pets_human_iou:g}"
    )


def image_count(conn: sqlite3.Connection, root_id: int | None = None) -> int:
    rc = " AND root_id=?" if root_id is not None else ""
    params = (root_id,) if root_id is not None else ()
    n: int = conn.execute(
        f"""SELECT COUNT(*) FROM files WHERE present=1 AND hidden=0
            AND media_type='image'{rc}""",
        params,
    ).fetchone()[0]
    return n


def pending_count(
    conn: sqlite3.Connection, root_id: int | None = None, model_source: str | None = None
) -> int:
    rc = " AND f.root_id=?" if root_id is not None else ""
    params: list[object] = []
    model_clause = ""
    if model_source is not None:
        model_clause = " OR s.model_source IS NOT ?"
        params.append(model_source)
    if root_id is not None:
        params.append(root_id)
    n: int = conn.execute(
        f"""SELECT COUNT(*) FROM files f
            LEFT JOIN pet_scan s ON s.file_id=f.id
            WHERE (s.file_id IS NULL OR s.source_sha256 IS NOT f.sha256
                   {model_clause})
              AND f.present=1 AND f.hidden=0 AND f.media_type='image'{rc}""",
        params,
    ).fetchone()[0]
    return n


def _pending(
    conn: sqlite3.Connection,
    limit: int,
    root_id: int | None = None,
    model_source: str | None = None,
) -> list[sqlite3.Row]:
    rc = " AND f.root_id=?" if root_id is not None else ""
    params: list[object] = []
    model_clause = ""
    if model_source is not None:
        model_clause = " OR s.model_source IS NOT ?"
        params.append(model_source)
    if root_id is not None:
        params.append(root_id)
    params.append(limit)
    return conn.execute(
        f"""SELECT f.id,f.rel_path,f.sha256,r.path root_path FROM files f
            JOIN roots r ON r.id=f.root_id
            LEFT JOIN pet_scan s ON s.file_id=f.id
            WHERE (s.file_id IS NULL OR s.source_sha256 IS NOT f.sha256
                   {model_clause})
              AND f.present=1 AND f.hidden=0 AND f.media_type='image'{rc}
            ORDER BY f.id LIMIT ?""",
        params,
    ).fetchall()


def make_backend(cfg: Config, log: Log | None = None) -> PetBackend:
    source = scan_source(cfg)
    return backend.PetBackend(
        cfg.cache_dir,
        min_score=cfg.pets_min_score,
        min_px=cfg.pets_min_px,
        max_side=cfg.pets_max_side,
        species=cfg.pets_species,
        human_min_score=cfg.pets_human_min_score,
        model_source=source,
        log=log,
    )


def _overlap_fraction(face: sqlite3.Row, animal: sqlite3.Row) -> float:
    left = max(face["box_x"], animal["box_x"])
    top = max(face["box_y"], animal["box_y"])
    right = min(face["box_x"] + face["box_w"], animal["box_x"] + animal["box_w"])
    bottom = min(face["box_y"] + face["box_h"], animal["box_y"] + animal["box_h"])
    # Annotated rather than returned directly: a sqlite3.Row indexes to Any, so
    # the arithmetic above is Any too and warn_return_any would let it out
    # through a signature promising a float.
    intersection: float = max(0, right - left) * max(0, bottom - top)
    area: float = max(1, face["box_w"] * face["box_h"])
    return intersection / area


def _extract_one(
    conn: sqlite3.Connection,
    be: PetBackend,
    row: sqlite3.Row,
    source: str,
    now: str,
    stats: PetExtractStats,
    progress: Progress | None,
) -> None:
    """Detect and store one image's animals, then mark it scanned either way.

    Mirrors ``faces.extract._extract_one``, down to marking the file scanned on
    the failure path: a file that cannot be read must not be retried on every
    pass for ever.
    """
    path = Path(row["root_path"]) / row["rel_path"]
    count = 0
    try:
        # Standalone CLI path: plain animal detection. The GUI's fused
        # detect stage (trove/detect) is what cross-checks the
        # faces; this only fills animal_detections for pet grouping.
        conn.execute("DELETE FROM animal_detections WHERE file_id=?", (row["id"],))
        detections = be.process_path(str(path))
        for detection in detections:
            conn.execute(
                """INSERT INTO animal_detections
                   (file_id,species,box_x,box_y,box_w,box_h,det_score,
                    embedding,model_source,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    row["id"],
                    detection.species,
                    detection.x,
                    detection.y,
                    detection.w,
                    detection.h,
                    detection.score,
                    detection.embedding.tobytes(),
                    source,
                    now,
                ),
            )
        count = len(detections)
    except Exception as error:
        # One line, no traceback: this loop runs over the whole archive
        # (~150k files) and a per-file exc_info would flood the rotated
        # log before anything useful survives it. error_samples still
        # keeps the first few failures verbatim for the CLI summary.
        logger.warning("pet extraction failed for %s: %s", path.name, error)
        stats.errors += 1
        if len(stats.error_samples) < 5:
            stats.error_samples.append(f"{path.name}: {error}")
    conn.execute(
        """INSERT OR REPLACE INTO pet_scan
           (file_id,n_animals,source_sha256,model_source,scanned_at)
           VALUES(?,?,?,?,?)""",
        (row["id"], count, row["sha256"], source, now),
    )
    stats.processed += 1
    stats.animals += count
    stats.photos_with_animals += int(count > 0)
    if progress is not None:
        progress.update(stats.processed, 0, path.name)


def extract(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    progress: Progress | None = None,
    limit: int | None = None,
    batch_size: int = 32,
    root_id: int | None = None,
    be: PetBackend | None = None,
) -> PetExtractStats:
    stats = PetExtractStats()
    if not backend.available():
        raise RuntimeError("pet backend unavailable (needs OpenCV DNN and NumPy)")
    if be is None:
        be = make_backend(cfg)
    source = scan_source(cfg)
    total = pending_count(conn, root_id, source)
    if limit is not None:
        total = min(total, limit)
    if progress is not None:
        progress.total = total
    now = db.now_iso()
    while True:
        remaining = None if limit is None else limit - stats.processed
        if remaining is not None and remaining <= 0:
            break
        rows = _pending(
            conn,
            batch_size if remaining is None else min(batch_size, remaining),
            root_id=root_id,
            model_source=source,
        )
        if not rows:
            break
        for row in rows:
            _extract_one(conn, be, row, source, now, stats, progress)
        conn.commit()
    return stats
