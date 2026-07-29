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
from . import backend, fiqa


@dataclass
class ExtractStats:
    processed: int = 0  # images examined this run
    faces_found: int = 0
    images_with_faces: int = 0
    errors: int = 0
    error_samples: list = None
    candidates: int = 0
    rejected_score: int = 0
    rejected_size: int = 0
    rejected_focus: int = 0
    rejected_exposure: int = 0
    rejected_clipped: int = 0
    rejected_nonhuman: int = 0

    def __post_init__(self):
        if self.error_samples is None:
            self.error_samples = []


# hidden=0 skips non-canonical duplicate copies: dedup (which runs before the
# face pass) flags them, and re-detecting faces in a duplicate photo is pure
# waste — the canonical copy is scanned instead.
def image_count(conn, root_id: int | None = None) -> int:
    """Total present, canonical (non-duplicate) images."""
    rc = " AND f.root_id=?" if root_id is not None else ""
    params = (root_id,) if root_id is not None else ()
    return conn.execute(
        f"""SELECT COUNT(*) FROM files f
            WHERE f.present=1 AND f.media_type='image' AND f.hidden=0{rc}""",
        params,
    ).fetchone()[0]


def pending_count(conn, root_id: int | None = None) -> int:
    """Present canonical images not yet face-scanned (optionally within a root)."""
    rc = " AND f.root_id=?" if root_id is not None else ""
    params = (root_id,) if root_id is not None else ()
    return conn.execute(
        f"""SELECT COUNT(*) FROM files f
            LEFT JOIN face_scan s ON s.file_id=f.id
            WHERE s.file_id IS NULL AND f.present=1 AND f.media_type='image'
                  AND f.hidden=0{rc}""",
        params,
    ).fetchone()[0]


def _pending(conn, batch_size: int):
    return conn.execute(
        """SELECT f.id, f.rel_path, f.sha256, r.path AS root_path
           FROM files f JOIN roots r ON r.id=f.root_id
           LEFT JOIN face_scan s ON s.file_id=f.id
           WHERE s.file_id IS NULL AND f.present=1 AND f.media_type='image'
                 AND f.hidden=0
           ORDER BY f.id
           LIMIT ?""",
        (batch_size,),
    ).fetchall()


def make_backend(cfg: Config, log=None) -> backend.FaceBackend:
    """Build the detector+embedder from config (loads the ONNX models once).
    Callers that extract in chunks pass the result back in to avoid reloading
    the models every chunk."""
    return backend.FaceBackend(
        cfg.cache_dir,
        min_score=cfg.faces_min_score,
        min_px=cfg.faces_min_px,
        max_side=cfg.faces_max_side,
        det_size=cfg.faces_det_size,
        min_focus=cfg.faces_min_focus,
        max_extreme_fraction=cfg.faces_max_extreme_fraction,
        max_clipped_fraction=cfg.faces_max_clipped_fraction,
        quality_version=cfg.faces_quality_version,
        log=log,
    )
    # The caller attaches the FIQA assessor (it needs a DB connection); until it
    # does, the backend tiers every face BORDERLINE, which is the safe default.


def _add_rejections(stats: ExtractStats, report) -> None:
    stats.candidates += report.candidates
    for reason in ("score", "size", "focus", "exposure", "clipped", "nonhuman"):
        setattr(
            stats,
            f"rejected_{reason}",
            getattr(stats, f"rejected_{reason}") + report.rejected.get(reason, 0),
        )


def quality_summary(conn, root_id: int | None = None) -> dict:
    """Aggregate extraction quality diagnostics for CLI/GUI reporting."""
    rc = " AND f.root_id=?" if root_id is not None else ""
    params = (root_id,) if root_id is not None else ()
    scan = conn.execute(
        f"""SELECT COUNT(*) images, COALESCE(SUM(s.n_candidates),0) candidates,
                   COALESCE(SUM(s.n_faces),0) accepted,
                   COALESCE(SUM(s.rejected_score),0) rejected_score,
                   COALESCE(SUM(s.rejected_size),0) rejected_size,
                   COALESCE(SUM(s.rejected_focus),0) rejected_focus,
                   COALESCE(SUM(s.rejected_exposure),0) rejected_exposure,
                   COALESCE(SUM(s.rejected_clipped),0) rejected_clipped,
                   COALESCE(SUM(s.rejected_nonhuman),0) rejected_nonhuman
            FROM face_scan s JOIN files f ON f.id=s.file_id
            WHERE 1=1{rc}""",
        params,
    ).fetchone()
    accepted = conn.execute(
        f"""SELECT AVG(fc.focus_score) avg_focus,
                   AVG(fc.brightness) avg_brightness,
                   AVG(fc.quality_score) avg_quality,
                   AVG(fc.fiqa_score) avg_fiqa,
                   AVG(fc.fiqa_norm) avg_fiqa_norm,
                   SUM(fc.person_id IS NULL AND fc.not_person=0) cluster_noise
            FROM faces fc JOIN files f ON f.id=fc.file_id
            WHERE 1=1{rc}""",
        params,
    ).fetchone()
    out = dict(scan)
    out.update(dict(accepted))
    # Per-tier counts make the FIQA gate auditable: LOW_QUALITY faces are hidden
    # from the GUI, so this summary is the place they remain visible at all.
    out["tiers"] = {t: 0 for t in fiqa.TIERS}
    out["tiers"]["UNTIERED"] = 0
    for r in conn.execute(
        f"""SELECT COALESCE(fc.quality_tier, 'UNTIERED') tier, COUNT(*) n
                FROM faces fc JOIN files f ON f.id=fc.file_id
                WHERE 1=1{rc} GROUP BY 1""",
        params,
    ):
        out["tiers"][r["tier"]] = r["n"]
    return out


def calibrate_quality(conn, cfg: Config, limit: int = 100, be=None, progress=None) -> ExtractStats:
    """Dry-run quality gates over pending images; never writes scan markers.

    Score and minimum-size filtering still apply. Focus/exposure/clipping gates
    are evaluated after detection so the report shows what the current
    thresholds *would* reject.
    """
    stats = ExtractStats()
    if be is None:
        be = make_backend(cfg)
    rows = _pending(conn, max(1, limit))
    if progress is not None:
        progress.total = len(rows)
    for row in rows:
        path = Path(row["root_path"]) / row["rel_path"]
        try:
            report = be.process_path_report(str(path), apply_quality_gate=False)
            stats.candidates += report.candidates
            stats.rejected_score += report.rejected.get("score", 0)
            stats.rejected_size += report.rejected.get("size", 0)
            for face in report.faces:
                if face.clipped_fraction > cfg.faces_max_clipped_fraction:
                    stats.rejected_clipped += 1
                elif face.focus_score < cfg.faces_min_focus:
                    stats.rejected_focus += 1
                elif face.extreme_fraction > cfg.faces_max_extreme_fraction:
                    stats.rejected_exposure += 1
                else:
                    stats.faces_found += 1
        except Exception as e:
            stats.errors += 1
            if len(stats.error_samples) < 5:
                stats.error_samples.append(f"{path.name}: {e}")
        stats.processed += 1
        if progress is not None:
            progress.update(stats.processed, 0, path.name)
    return stats


def extract(
    conn, cfg: Config, progress=None, batch_size: int = 64, limit: int | None = None, be=None
) -> ExtractStats:
    """Detect faces for pending images. ``limit`` caps images this run (useful
    for chunked runs / testing); None means "until none are left". Pass ``be``
    to reuse an already-loaded backend across calls."""
    stats = ExtractStats()
    if not backend.available():
        raise RuntimeError("face backend unavailable (needs OpenCV DNN face APIs)")

    if be is None:
        be = make_backend(
            cfg, log=(lambda m: progress.update(0, 0, m)) if progress is not None else None
        )

    total = pending_count(conn)
    if limit is not None:
        total = min(total, limit)
    if progress is not None:
        progress.total = total

    # Attached per run, not baked into the backend: the calibration row this
    # depends on may be created by this very run (see the bootstrap below).
    if hasattr(be, "assessor"):
        be.assessor = fiqa.make_assessor(conn, cfg)

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
            report = backend.DetectionReport()
            try:
                if hasattr(be, "process_path_report"):
                    report = be.process_path_report(str(path))
                    faces = report.faces
                else:  # compatibility with lightweight third-party/test backends
                    faces = be.process_path(str(path))
                    report.faces = faces
                    report.candidates = len(faces)
                # Standalone CLI path: plain face detection, no animal cross-check.
                # The GUI's fused detect stage (organize_archive/detect) is what
                # drops animal-face false positives, since it has the animal boxes
                # in hand from the same decode.
                for fc in faces:
                    conn.execute(
                        """INSERT INTO faces
                           (file_id, box_x, box_y, box_w, box_h, det_score,
                            focus_score, brightness, extreme_fraction,
                            clipped_fraction, quality_score, quality_source,
                            fiqa_norm, fiqa_score, fiqa_source, quality_tier,
                            embedding, created_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            fid,
                            fc.x,
                            fc.y,
                            fc.w,
                            fc.h,
                            fc.score,
                            getattr(fc, "focus_score", None),
                            getattr(fc, "brightness", None),
                            getattr(fc, "extreme_fraction", None),
                            getattr(fc, "clipped_fraction", None),
                            getattr(fc, "quality_score", None),
                            getattr(fc, "quality_source", None),
                            getattr(fc, "fiqa_norm", None),
                            getattr(fc, "fiqa_score", None),
                            getattr(getattr(be, "assessor", None), "model", None),
                            getattr(fc, "quality_tier", None),
                            fc.embedding.tobytes(),
                            now,
                        ),
                    )
                n_faces = len(faces)
                _add_rejections(stats, report)
            except Exception as e:  # bad/corrupt image, unreadable, etc.
                stats.errors += 1
                if len(stats.error_samples) < 5:
                    stats.error_samples.append(f"{path.name}: {e}")

            conn.execute(
                """INSERT OR REPLACE INTO face_scan
                   (file_id, n_faces, n_candidates, rejected_score, rejected_size,
                    rejected_focus, rejected_exposure, rejected_clipped,
                    rejected_nonhuman, scanned_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    fid,
                    n_faces,
                    report.candidates,
                    report.rejected.get("score", 0),
                    report.rejected.get("size", 0),
                    report.rejected.get("focus", 0),
                    report.rejected.get("exposure", 0),
                    report.rejected.get("clipped", 0),
                    report.rejected.get("nonhuman", 0),
                    now,
                ),
            )
            stats.processed += 1
            stats.faces_found += n_faces
            if n_faces:
                stats.images_with_faces += 1
            if progress is not None:
                progress.update(stats.processed, 0, path.name)

        conn.commit()

        # Fix the FIQA calibration once enough norms exist, then tier the
        # backlog extracted before it (see faces/fiqa.py). A no-op thereafter.
        if hasattr(be, "assessor"):
            if (
                fiqa.load_calibration(conn, cfg.faces_fiqa_model) is None
                and fiqa.bootstrap_calibration(conn, cfg) is not None
            ):
                conn.commit()
                be.assessor = fiqa.make_assessor(conn, cfg)

    return stats
