"""Face extraction: detect faces + embeddings for indexed images.

Resumable and incremental, like enrich: only present images without a
``face_scan`` row are processed, in batches, and a photo with no faces still
gets a (n_faces=0) row so it is never re-examined. Read-only over originals.

Clustering into people is a separate step (faces/cluster.py); this only fills
the ``faces`` / ``face_scan`` tables.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import Config
from ..db import database as db
from . import backend


@dataclass
class ExtractStats:
    processed: int = 0          # images examined this run
    faces_found: int = 0
    images_with_faces: int = 0
    errors: int = 0
    error_samples: list = None

    def __post_init__(self):
        if self.error_samples is None:
            self.error_samples = []


def pending_count(conn, root_id: int | None = None) -> int:
    """Present images not yet face-scanned (optionally within one root)."""
    rc = " AND f.root_id=?" if root_id is not None else ""
    params = (root_id,) if root_id is not None else ()
    return conn.execute(
        f"""SELECT COUNT(*) FROM files f
            LEFT JOIN face_scan s ON s.file_id=f.id
            WHERE s.file_id IS NULL AND f.present=1 AND f.media_type='image'{rc}""",
        params).fetchone()[0]


def _pending(conn, batch_size: int):
    return conn.execute(
        """SELECT f.id, f.rel_path, r.path AS root_path
           FROM files f JOIN roots r ON r.id=f.root_id
           LEFT JOIN face_scan s ON s.file_id=f.id
           WHERE s.file_id IS NULL AND f.present=1 AND f.media_type='image'
           ORDER BY f.id
           LIMIT ?""",
        (batch_size,)).fetchall()


def make_backend(cfg: Config, log=None) -> "backend.FaceBackend":
    """Build the detector+embedder from config (loads the ONNX models once).
    Callers that extract in chunks pass the result back in to avoid reloading
    the models every chunk."""
    return backend.FaceBackend(
        cfg.cache_dir, min_score=cfg.faces_min_score, min_px=cfg.faces_min_px,
        max_side=cfg.faces_max_side, embed_backend=cfg.faces_embed_backend, log=log)


def extract(conn, cfg: Config, progress=None, batch_size: int = 64,
            limit: int | None = None, be=None) -> ExtractStats:
    """Detect faces for pending images. ``limit`` caps images this run (useful
    for chunked runs / testing); None means "until none are left". Pass ``be``
    to reuse an already-loaded backend across calls."""
    stats = ExtractStats()
    if not backend.available():
        raise RuntimeError("face backend unavailable (needs OpenCV DNN face APIs)")

    if be is None:
        be = make_backend(
            cfg, log=(lambda m: progress.update(0, 0, m)) if progress is not None else None)

    total = pending_count(conn)
    if limit is not None:
        total = min(total, limit)
    if progress is not None:
        progress.total = total

    now = db.now_iso()
    while True:
        remaining = None if limit is None else max(0, limit - stats.processed)
        if remaining == 0:
            break
        rows = _pending(conn, batch_size if remaining is None else min(batch_size, remaining))
        if not rows:
            break

        for row in rows:
            fid = row["id"]
            path = Path(row["root_path"]) / row["rel_path"]
            n_faces = 0
            try:
                faces = be.process_path(str(path))
                for fc in faces:
                    conn.execute(
                        """INSERT INTO faces
                           (file_id, box_x, box_y, box_w, box_h, det_score,
                            embedding, created_at)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (fid, fc.x, fc.y, fc.w, fc.h, fc.score,
                         fc.embedding.tobytes(), now))
                n_faces = len(faces)
            except Exception as e:  # bad/corrupt image, unreadable, etc.
                stats.errors += 1
                if len(stats.error_samples) < 5:
                    stats.error_samples.append(f"{path.name}: {e}")

            conn.execute(
                """INSERT OR REPLACE INTO face_scan (file_id, n_faces, scanned_at)
                   VALUES (?,?,?)""", (fid, n_faces, now))
            stats.processed += 1
            stats.faces_found += n_faces
            if n_faces:
                stats.images_with_faces += 1
            if progress is not None:
                progress.update(stats.processed, 0, path.name)

        conn.commit()

    return stats
