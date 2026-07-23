"""Resumable animal extraction and face/non-human reconciliation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from ..db import database as db
from . import backend


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
        f"minpx={cfg.pets_min_px};side={cfg.pets_max_side};species={species}")


def image_count(conn, root_id=None):
    rc = " AND root_id=?" if root_id is not None else ""
    params = (root_id,) if root_id is not None else ()
    return conn.execute(
        f"""SELECT COUNT(*) FROM files WHERE present=1 AND hidden=0
            AND media_type='image'{rc}""", params).fetchone()[0]


def pending_count(conn, root_id=None, model_source=None):
    rc = " AND f.root_id=?" if root_id is not None else ""
    params = []
    model_clause = ""
    if model_source is not None:
        model_clause = " OR s.model_source IS NOT ?"
        params.append(model_source)
    if root_id is not None:
        params.append(root_id)
    return conn.execute(
        f"""SELECT COUNT(*) FROM files f
            LEFT JOIN pet_scan s ON s.file_id=f.id
            WHERE (s.file_id IS NULL OR s.source_sha256 IS NOT f.sha256
                   {model_clause})
              AND f.present=1 AND f.hidden=0 AND f.media_type='image'{rc}""",
        params).fetchone()[0]


def _pending(conn, limit, root_id=None, model_source=None):
    rc = " AND f.root_id=?" if root_id is not None else ""
    params = []
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
            ORDER BY f.id LIMIT ?""", params).fetchall()


def make_backend(cfg: Config, log=None):
    source = scan_source(cfg)
    return backend.PetBackend(
        cfg.cache_dir, min_score=cfg.pets_min_score, min_px=cfg.pets_min_px,
        max_side=cfg.pets_max_side, species=cfg.pets_species,
        model_source=source, log=log)


def _overlap_fraction(face, animal) -> float:
    left = max(face["box_x"], animal["box_x"])
    top = max(face["box_y"], animal["box_y"])
    right = min(face["box_x"] + face["box_w"], animal["box_x"] + animal["box_w"])
    bottom = min(face["box_y"] + face["box_h"], animal["box_y"] + animal["box_h"])
    intersection = max(0, right - left) * max(0, bottom - top)
    area = max(1, face["box_w"] * face["box_h"])
    return intersection / area


def suppress_unassigned_faces(conn, file_id: int, threshold: float,
                                model_source: str,
                                source_sha256: str | None) -> int:
    """Suppress only unassigned automatic faces; never override a user pin/name."""
    animals = conn.execute(
        "SELECT * FROM animal_detections WHERE file_id=?", (file_id,)).fetchall()
    faces = conn.execute(
        """SELECT * FROM faces WHERE file_id=? AND person_id IS NULL
           AND manual_person IS NULL AND not_person=0""", (file_id,)).fetchall()
    overrides = conn.execute(
        """SELECT * FROM nonhuman_detections
           WHERE file_id=? AND review_status='human' AND source_sha256 IS ?""",
        (file_id, source_sha256)).fetchall()
    suppressed = 0
    for face in faces:
        if any(_overlap_fraction(face, override) >= 0.80
               for override in overrides):
            continue
        match = next((animal for animal in animals
                      if _overlap_fraction(face, animal) >= threshold), None)
        if match is None:
            continue
        kind = "toy" if match["species"] == "teddy bear" else "animal"
        source = f"animal-overlap:{model_source}"
        conn.execute(
            """UPDATE faces SET not_person=1,nonhuman_kind=?,
                                nonhuman_source=? WHERE id=?""",
            (kind, source, face["id"]))
        conn.execute(
            """UPDATE face_scan SET n_faces=MAX(0,n_faces-1),
                   rejected_nonhuman=rejected_nonhuman+1 WHERE file_id=?""",
            (file_id,))
        conn.execute(
            """INSERT INTO nonhuman_detections
               (file_id,animal_detection_id,box_x,box_y,box_w,box_h,kind,
                confidence,source,source_sha256,embedding,det_score,focus_score,brightness,
                extreme_fraction,clipped_fraction,quality_score,quality_source,
                restored_face_id,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (file_id, match["id"], face["box_x"], face["box_y"], face["box_w"],
             face["box_h"], kind, min(face["det_score"], match["det_score"]),
             source, source_sha256, face["embedding"], face["det_score"],
             face["focus_score"],
             face["brightness"], face["extreme_fraction"],
             face["clipped_fraction"], face["quality_score"],
             face["quality_source"], face["id"], db.now_iso()))
        suppressed += 1
    return suppressed


def extract(conn, cfg: Config, *, progress=None, limit=None, batch_size=32,
            root_id=None, be=None) -> PetExtractStats:
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
            conn, batch_size if remaining is None else min(batch_size, remaining),
            root_id=root_id, model_source=source)
        if not rows:
            break
        for row in rows:
            path = Path(row["root_path"]) / row["rel_path"]
            count = 0
            try:
                previous = conn.execute(
                    "SELECT source_sha256 FROM pet_scan WHERE file_id=?",
                    (row["id"],)).fetchone()
                if previous and previous["source_sha256"] == row["sha256"]:
                    conn.execute(
                        """DELETE FROM nonhuman_detections
                           WHERE file_id=? AND review_status='pending'""",
                        (row["id"],))
                else:
                    conn.execute(
                        "DELETE FROM nonhuman_detections WHERE file_id=?",
                        (row["id"],))
                conn.execute(
                    "DELETE FROM animal_detections WHERE file_id=?", (row["id"],))
                detections = be.process_path(str(path))
                for detection in detections:
                    conn.execute(
                        """INSERT INTO animal_detections
                           (file_id,species,box_x,box_y,box_w,box_h,det_score,
                            embedding,model_source,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (row["id"], detection.species, detection.x, detection.y,
                         detection.w, detection.h, detection.score,
                         detection.embedding.tobytes(), source, now))
                count = len(detections)
                stats.faces_suppressed += suppress_unassigned_faces(
                    conn, row["id"], cfg.pets_face_overlap,
                    source, row["sha256"])
            except Exception as error:
                stats.errors += 1
                if len(stats.error_samples) < 5:
                    stats.error_samples.append(f"{path.name}: {error}")
            conn.execute(
                """INSERT OR REPLACE INTO pet_scan
                   (file_id,n_animals,source_sha256,model_source,scanned_at)
                   VALUES(?,?,?,?,?)""",
                (row["id"], count, row["sha256"], source, now))
            stats.processed += 1
            stats.animals += count
            stats.photos_with_animals += int(count > 0)
            if progress is not None:
                progress.update(stats.processed, 0, path.name)
        conn.commit()
    return stats
