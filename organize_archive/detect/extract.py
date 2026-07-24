"""Fused detection: find people (SCRFD) and animals (YOLOX) in ONE image pass.

The old pipeline detected pets and faces in two separate stages, each decoding
every photo independently — so ~150k images were decoded twice. Here each image
is decoded a single time (at ``cfg.detect_max_side``) and both detectors run on
that one array. The animal boxes then cross-check the faces inline: a face mostly
inside an animal box is an animal-face false positive and is dropped from People.
That single rule replaces the old sprawl (a separate suppression sweep, a learned
non-human k-NN, and a ``nonhuman_detections`` review limbo).

Resumable and incremental, like the stages it replaces: an image is pending when
it lacks a current ``face_scan`` OR a current ``pet_scan`` row, and is processed
as a unit (both detectors run, both ``faces`` and ``animal_detections`` for the
file are rewritten), so the cross-check is always consistent. Read-only over
originals. Clustering into people/pets is a separate step (faces/pets cluster.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from ..db import database as db
from ..faces import backend as face_backend
from ..pets import backend as pet_backend
from ..pets.extract import scan_source as pet_scan_source


@dataclass
class DetectStats:
    processed: int = 0             # images examined this run
    faces_found: int = 0
    images_with_faces: int = 0
    animals: int = 0
    photos_with_animals: int = 0
    nonhuman_suppressed: int = 0   # faces dropped for overlapping an animal box
    candidates: int = 0
    rejected_score: int = 0
    rejected_size: int = 0
    rejected_clipped: int = 0
    rejected_nonhuman: int = 0
    errors: int = 0
    error_samples: list = field(default_factory=list)


# hidden=0 skips non-canonical duplicate copies (dedup runs before detection);
# re-detecting a duplicate is pure waste — the canonical copy is scanned instead.
_PENDING_WHERE = """
    f.present=1 AND f.media_type='image' AND f.hidden=0
    AND (fs.file_id IS NULL
         OR ps.file_id IS NULL
         OR ps.source_sha256 IS NOT f.sha256
         OR ps.model_source IS NOT :pet_src)
"""


def image_count(conn, root_id: int | None = None) -> int:
    """Total present, canonical (non-duplicate) images."""
    rc = " AND f.root_id=?" if root_id is not None else ""
    params = (root_id,) if root_id is not None else ()
    return conn.execute(
        f"""SELECT COUNT(*) FROM files f
            WHERE f.present=1 AND f.media_type='image' AND f.hidden=0{rc}""",
        params).fetchone()[0]


def pending_count(conn, cfg: Config, root_id: int | None = None) -> int:
    """Present canonical images missing a current face OR pet scan."""
    rc = " AND f.root_id=:root" if root_id is not None else ""
    p = {"pet_src": pet_scan_source(cfg)}
    if root_id is not None:
        p["root"] = root_id
    return conn.execute(
        f"""SELECT COUNT(*) FROM files f
            LEFT JOIN face_scan fs ON fs.file_id=f.id
            LEFT JOIN pet_scan ps ON ps.file_id=f.id
            WHERE {_PENDING_WHERE}{rc}""", p).fetchone()[0]


def _pending(conn, cfg: Config, batch_size: int):
    return conn.execute(
        f"""SELECT f.id, f.rel_path, f.sha256, r.path AS root_path,
                   (fs.file_id IS NULL) AS need_face
            FROM files f JOIN roots r ON r.id=f.root_id
            LEFT JOIN face_scan fs ON fs.file_id=f.id
            LEFT JOIN pet_scan ps ON ps.file_id=f.id
            WHERE {_PENDING_WHERE}
            ORDER BY f.id
            LIMIT :lim""",
        {"pet_src": pet_scan_source(cfg), "lim": batch_size}).fetchall()


def available() -> bool:
    """True if at least one detector can run (faces and/or pets)."""
    return face_backend.available() or pet_backend.available()


def make_backends(cfg: Config, log=None):
    """Build whichever detectors are available (loads the ONNX models once).

    Returns ``(face_be, pet_be)``; either may be None when its optional deps or
    models are missing, so the stage degrades gracefully (faces-only or pets-only)
    instead of failing the whole pass."""
    face_be = pet_be = None
    if face_backend.available():
        face_be = face_backend.FaceBackend(
            cfg.cache_dir, min_score=cfg.faces_min_score, min_px=cfg.faces_min_px,
            max_side=cfg.faces_max_side, det_size=cfg.faces_det_size,
            max_clipped_fraction=cfg.faces_max_clipped_fraction,
            min_focus=cfg.faces_min_focus,
            max_extreme_fraction=cfg.faces_max_extreme_fraction,
            quality_version=cfg.faces_quality_version, log=log)
    if pet_backend.available():
        pet_be = pet_backend.PetBackend(
            cfg.cache_dir, min_score=cfg.pets_min_score, min_px=cfg.pets_min_px,
            max_side=cfg.pets_max_side, species=cfg.pets_species,
            model_source=pet_scan_source(cfg), log=log)
    return face_be, pet_be


def _load_bgr(path: str, max_side: int):
    """Decode ONCE to a BGR array + uniform scale (detected/original ≤ 1).

    Mirrors faces.backend.load_bgr: Pillow so HEIC + EXIF orientation are handled,
    ``draft()`` for fast JPEG downscale-on-decode, one uniform ``scale`` (captured
    against the true on-disk size, before draft shrinks it) so BOTH detectors map
    their boxes back to real original pixels. Read-only over the original.
    """
    import cv2
    import numpy as np
    from PIL import Image, ImageOps
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass
    with Image.open(path) as im:
        orig_side = max(im.size)
        try:
            im.draft("RGB", (max_side, max_side))
        except Exception:
            pass
        im = ImageOps.exif_transpose(im).convert("RGB")
        w, h = im.size
        rs = min(1.0, max_side / max(w, h)) if max(w, h) else 1.0
        if rs < 1.0:
            im = im.resize((max(1, round(w * rs)), max(1, round(h * rs))))
        arr = np.asarray(im)
    scale = (max(arr.shape[:2]) / orig_side) if orig_side else 1.0
    return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR), scale


def _overlap_fraction(fx, fy, fw, fh, ax, ay, aw, ah) -> float:
    """Fraction of the FACE box that lies inside the animal box."""
    left = max(fx, ax)
    top = max(fy, ay)
    right = min(fx + fw, ax + aw)
    bottom = min(fy + fh, ay + ah)
    inter = max(0, right - left) * max(0, bottom - top)
    return inter / max(1, fw * fh)


def extract(conn, cfg: Config, *, progress=None, batch_size: int = 32,
            limit: int | None = None, face_be=None, pet_be=None) -> DetectStats:
    """Detect people + animals for pending images in one decode each.

    ``limit`` caps images this run (chunked runs / testing); None = until drained.
    Pass already-loaded backends to reuse them across chunks. Either backend may be
    None (feature unavailable): the pass then does only the other, and with no pet
    backend there is simply no animal cross-check.
    """
    stats = DetectStats()
    if not available():
        raise RuntimeError("detect backend unavailable (needs faces and/or pets)")
    if face_be is None and pet_be is None:
        face_be, pet_be = make_backends(
            cfg, log=(lambda m: progress.update(0, 0, m)) if progress else None)

    total = pending_count(conn, cfg)
    if limit is not None:
        total = min(total, limit)
    if progress is not None:
        progress.total = total

    now = db.now_iso()
    pet_src = pet_scan_source(cfg)
    while True:
        remaining = None if limit is None else max(0, limit - stats.processed)
        if remaining == 0:
            break
        rows = _pending(
            conn, cfg, batch_size if remaining is None else min(batch_size, remaining))
        if not rows:
            break

        for row in rows:
            fid = row["id"]
            path = Path(row["root_path"]) / row["rel_path"]
            n_faces = n_animals = 0
            face_report = face_backend.DetectionReport()
            try:
                img, scale = _load_bgr(str(path), cfg.detect_max_side)
                inv = 1.0 / scale if scale else 1.0

                # -- animals (YOLOX): map boxes to ORIGINAL pixels ------------
                animals = []
                if pet_be is not None:
                    for a in pet_be.detect(img):
                        a.x = max(0, round(a.x * inv))
                        a.y = max(0, round(a.y * inv))
                        a.w = round(a.w * inv)
                        a.h = round(a.h * inv)
                        animals.append(a)

                # -- faces (SCRFD): already mapped to ORIGINAL via scale ------
                if face_be is not None:
                    face_report = face_be.detect_report(img, scale)

                # -- inline cross-check: drop faces inside an animal box ------
                human_faces = []
                for fc in face_report.faces:
                    if any(_overlap_fraction(fc.x, fc.y, fc.w, fc.h,
                                             a.x, a.y, a.w, a.h) >= cfg.pets_face_overlap
                           for a in animals):
                        face_report.rejected["nonhuman"] = (
                            face_report.rejected.get("nonhuman", 0) + 1)
                        stats.nonhuman_suppressed += 1
                        continue
                    human_faces.append(fc)

                # -- rewrite this file's detections as a unit ----------------
                conn.execute("DELETE FROM faces WHERE file_id=?", (fid,))
                conn.execute("DELETE FROM animal_detections WHERE file_id=?", (fid,))
                conn.execute("DELETE FROM nonhuman_detections WHERE file_id=?", (fid,))
                for a in animals:
                    conn.execute(
                        """INSERT INTO animal_detections
                           (file_id,species,box_x,box_y,box_w,box_h,det_score,
                            embedding,model_source,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (fid, a.species, a.x, a.y, a.w, a.h, a.score,
                         a.embedding.tobytes(), pet_src, now))
                for fc in human_faces:
                    conn.execute(
                        """INSERT INTO faces
                           (file_id, box_x, box_y, box_w, box_h, det_score,
                            focus_score, brightness, extreme_fraction,
                            clipped_fraction, quality_score, quality_source,
                            embedding, created_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (fid, fc.x, fc.y, fc.w, fc.h, fc.score,
                         fc.focus_score, fc.brightness, fc.extreme_fraction,
                         fc.clipped_fraction, fc.quality_score, fc.quality_source,
                         fc.embedding.tobytes(), now))

                n_faces = len(human_faces)
                n_animals = len(animals)
                stats.candidates += face_report.candidates
                stats.rejected_score += face_report.rejected.get("score", 0)
                stats.rejected_size += face_report.rejected.get("size", 0)
                stats.rejected_clipped += face_report.rejected.get("clipped", 0)
                stats.rejected_nonhuman += face_report.rejected.get("nonhuman", 0)
            except Exception as e:  # bad/corrupt image, unreadable, etc.
                stats.errors += 1
                if len(stats.error_samples) < 5:
                    stats.error_samples.append(f"{path.name}: {e}")

            # Both scan markers, always written together so the image is never
            # reprocessed for one detector while the other's row is stale.
            conn.execute(
                """INSERT OR REPLACE INTO face_scan
                   (file_id, n_faces, n_candidates, rejected_score, rejected_size,
                    rejected_focus, rejected_exposure, rejected_clipped,
                    rejected_nonhuman, scanned_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (fid, n_faces, face_report.candidates,
                 face_report.rejected.get("score", 0),
                 face_report.rejected.get("size", 0), 0, 0,
                 face_report.rejected.get("clipped", 0),
                 face_report.rejected.get("nonhuman", 0), now))
            conn.execute(
                """INSERT OR REPLACE INTO pet_scan
                   (file_id, n_animals, source_sha256, model_source, scanned_at)
                   VALUES (?,?,?,?,?)""",
                (fid, n_animals, row["sha256"], pet_src, now))

            stats.processed += 1
            stats.faces_found += n_faces
            stats.animals += n_animals
            if n_faces:
                stats.images_with_faces += 1
            if n_animals:
                stats.photos_with_animals += 1
            if progress is not None:
                progress.update(stats.processed, 0, path.name)

        conn.commit()

    return stats
