"""Fused detection: find people (SCRFD) and animals (YOLOX) in ONE image pass.

The old pipeline detected pets and faces in two separate stages, each decoding
every photo independently — so ~150k images were decoded twice. Here each image
is decoded a single time (at ``cfg.detect_max_side``) and both detectors run on
that one array.

The cross-check between them is anchored on one signal: the COCO ``person`` box
that the same YOLOX pass already produces (see pets/backend.py). It arbitrates
in both directions, because both detectors fail on humans who are not vertical
in the frame — a photo stored sideways, or someone lying down:

* an animal box that *is* a person box (high IoU) is a misread human, not a pet.
  YOLOX calls a non-vertical person ``dog`` with real confidence; rotating the
  same photo upright flips it back to ``person``, so when nothing vetoes an
  animal box upright the box is re-tested on the quarter-turns before it is
  believed (only for the few images that have animal boxes at all).
* a face inside a person box is human and is never suppressed; a face inside an
  animal box with no person over it is an animal's face and is dropped from
  People.

That replaces the old one-directional rule, which dropped any face merely
*contained* in an animal box — so a bogus full-frame "dog" over a reclining
person deleted the real face from People *and* kept the phantom pet.

The same signal gives the stage its other job: **resolving true orientation**.
EXIF is applied on decode and settles most photos, but this archive is full of
re-exports whose pixels are turned while their orientation tag says they are
not, and on those every model here fails — SCRFD finds no face at all and the
person becomes a ``dog``. A ``person`` that reads clearly at a quarter turn and
not upright says which way up the photo really is; it is recorded in
``orientation`` and detection is redone there, so the boxes and the app's view
of the photo are both upright. Deliberately narrow (see ``_resolve_rotation``):
turning a correctly stored photo over is worse than leaving a sideways one
alone, so only a frame-filling subject that out-reads the animal score moves.

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
    nonhuman_suppressed: int = 0   # faces dropped as an animal's own face
    human_animals_dropped: int = 0  # "pets" that a person box exposed as people
    rotated: int = 0               # photos found to be stored sideways
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
            human_min_score=cfg.pets_human_min_score,
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
    """Fraction of the FIRST box that lies inside the second."""
    left = max(fx, ax)
    top = max(fy, ay)
    right = min(fx + fw, ax + aw)
    bottom = min(fy + fh, ay + ah)
    inter = max(0, right - left) * max(0, bottom - top)
    return inter / max(1, fw * fh)


def _iou(a, b) -> float:
    """Intersection over union of two boxes that expose .x/.y/.w/.h.

    IoU, not containment: two boxes only score high here when they describe the
    *same object*. A person holding a cat contains the cat's box but overlaps it
    poorly; a person misread as a dog produces two near-identical boxes.
    """
    left, top = max(a.x, b.x), max(a.y, b.y)
    right, bottom = min(a.x + a.w, b.x + b.w), min(a.y + a.h, b.y + b.h)
    inter = max(0, right - left) * max(0, bottom - top)
    union = a.w * a.h + b.w * b.h - inter
    return inter / max(1, union)


def _rotate_boxes_back(humans, k: int, w: int, h: int):
    """Map boxes found in a ``np.rot90(img, k)`` frame back to the upright one.

    ``k`` counts counter-clockwise quarter turns; ``w``/``h`` are the *upright*
    frame's size. Under k=1 an upright point (x, y) lands at (y, w-1-x), so a
    rotated box (xr, yr, wr, hr) came from (w-yr-hr, xr, hr, wr); k=3 is the
    mirror of that.
    """
    from ..pets.backend import HumanDetection
    out = []
    for d in humans:
        if k == 1:
            x, y, bw, bh = w - (d.y + d.h), d.x, d.h, d.w
        else:  # k == 3
            x, y, bw, bh = d.y, h - (d.x + d.w), d.h, d.w
        out.append(HumanDetection(x=x, y=y, w=bw, h=bh, score=d.score))
    return out


def _human_boxes_on_turns(img, pet_be):
    """Person boxes from both quarter-turns, mapped back to the upright frame.

    Only called for images that still hold an unvetoed animal box — a few
    percent of the archive. The extra passes are YOLOX-only and reuse the array
    already in memory, so no image is ever decoded twice.
    """
    import numpy as np
    h, w = img.shape[:2]
    found = []
    for k in (1, 3):
        humans = pet_be.detect_humans(np.ascontiguousarray(np.rot90(img, k)))
        found.extend(_rotate_boxes_back(humans, k, w, h))
    return found


def _drop_human_animals(animals, humans, min_iou: float, *, outscore=False):
    """Split animal boxes into (real animals, boxes that are really people).

    ``outscore`` additionally requires the person box to read at least as
    strongly as the animal it would overturn. Evidence found only by turning the
    image is worth less — a bird in flight or a cat curled up will read as a
    person from some angle — so a confident animal is left alone unless the
    person reading matches it.
    """
    kept, human_like = [], []
    for a in animals:
        if any(_iou(a, p) >= min_iou and (not outscore or p.score >= a.score)
               for p in humans):
            human_like.append(a)
        else:
            kept.append(a)
    return kept, human_like


# Clockwise display degrees <-> counter-clockwise np.rot90 turns.
_TURNS = {90: 3, 180: 2, 270: 1}


def rotate_image(img, deg: int):
    """Rotate a decoded array clockwise by 0/90/180/270 degrees."""
    if not deg:
        return img
    import numpy as np
    return np.ascontiguousarray(np.rot90(img, _TURNS[deg]))


@dataclass
class _Found:
    """Everything one decoded frame yielded, already cross-checked."""

    faces: list = field(default_factory=list)      # kept, human
    animals: list = field(default_factory=list)    # kept, real animals
    humans: list = field(default_factory=list)     # person boxes (context)
    report: object = None                          # the raw DetectionReport
    human_animals: int = 0                         # pets that were really people
    suppressed_faces: int = 0                      # animals' own faces
    max_subject_share: float = 0.0                 # biggest box, as a frame share
    animal_score: float = 0.0                      # best animal reading, pre-veto


def _detect_on(img, scale, cfg: Config, face_be, pet_be) -> _Found:
    """Run both detectors over one frame and cross-check them.

    Boxes come out in the frame's own full-resolution pixels (``1/scale``), so
    a caller that rotated ``img`` first gets boxes in that rotated frame — which
    is exactly where the app draws them.
    """
    inv = 1.0 / scale if scale else 1.0
    found = _Found(report=face_backend.DetectionReport())

    if pet_be is not None:
        animals, humans = pet_be.detect_with_humans(img)
        # Kept from before the veto: how confidently the photo reads as an
        # animal the way it is stored, which the orientation check weighs
        # against a person reading from some other angle.
        found.animal_score = max((a.score for a in animals), default=0.0)
        h, w = img.shape[:2]
        found.max_subject_share = max(
            (b.w * b.h for b in (*animals, *humans)), default=0) / max(1, w * h)
        animals, human_like = _drop_human_animals(
            animals, humans, cfg.pets_human_iou)
        if animals:
            # Survivors may still be people who are simply not vertical here —
            # the quarter-turns are where YOLOX reads them as `person` again.
            turn_humans = _human_boxes_on_turns(img, pet_be)
            humans += turn_humans
            animals, turned = _drop_human_animals(
                animals, turn_humans, cfg.pets_human_iou, outscore=True)
            human_like += turned
        found.human_animals = len(human_like)
        for box in (*animals, *humans):     # -> this frame's full-res pixels
            box.x = max(0, round(box.x * inv))
            box.y = max(0, round(box.y * inv))
            box.w = round(box.w * inv)
            box.h = round(box.h * inv)
        found.animals, found.humans = animals, humans

    if face_be is not None:
        found.report = face_be.detect_report(img, scale)

    for fc in found.report.faces:
        in_animal = any(
            _overlap_fraction(fc.x, fc.y, fc.w, fc.h, a.x, a.y, a.w, a.h)
            >= cfg.pets_face_overlap for a in found.animals)
        in_person = any(
            _overlap_fraction(fc.x, fc.y, fc.w, fc.h, p.x, p.y, p.w, p.h)
            >= cfg.pets_face_overlap for p in found.humans)
        if in_animal and not in_person:
            found.report.rejected["nonhuman"] = (
                found.report.rejected.get("nonhuman", 0) + 1)
            found.suppressed_faces += 1
            continue
        found.faces.append(fc)
    return found


def _best_person(pet_be, img) -> tuple[float, float]:
    """``(confidence, share of the frame)`` of the most confident person box."""
    h, w = img.shape[:2]
    frame = max(1, w * h)
    best = max(pet_be.detect_humans(img), key=lambda p: p.score, default=None)
    return (best.score, best.w * best.h / frame) if best else (0.0, 0.0)


def _resolve_rotation(img, pet_be, cfg: Config, subject_share: float,
                      animal_score: float):
    """Find the quarter turn that makes this photo's subject upright.

    The evidence is a YOLOX ``person`` reading that appears at a quarter turn
    and *not* upright. That asymmetry is the signature of a sideways-stored
    photo of a person, and it is what the pet cross-check already exploits.

    Being wrong here is expensive — turning a correctly stored photo over is
    worse than leaving a sideways one alone — so every guard below earns its
    place against a real counterexample from this archive:

    * **The subject must fill the frame.** Someone lying on the grass in a
      landscape shot reads as an upright person once turned, exactly like a
      standing person in a sideways photo. The difference is that the sideways
      portrait's subject covers most of the frame (measured: 0.93) while the
      person lying down is a detail of a scene (0.03) whose own horizon settles
      the question.
    * **Only landscape becomes portrait.** A person filling the frame is a
      portrait composition, so the frame that needs turning is the one stored
      the wrong way round — landscape. Without this, a close-up of someone
      lying in bed (already portrait, subject filling it, and indistinguishable
      from a sideways photo by every other measure) gets turned on its side.
    * **It must beat the animal reading.** A cat close-up reads as a mediocre
      ``person`` from some angle; if the photo is a more confident animal the
      way it is stored, nothing moves.
    * **Only the quarter turns.** Pixels stored upside down are vanishingly
      rare, while detectors happily fire on upside-down subjects.

    Faces are deliberately *not* used to decide this. On this archive a face
    that only appears when the photo is turned is nearly always a doll, a cake
    figurine or a person lying down rather than a sideways photo — the signal
    is there, but its precision is not good enough to move photos on.

    Returns ``(degrees, confidence, source)``, or ``(0, 0.0, "")`` to leave the
    photo untouched.
    """
    # Three YOLOX passes — the most expensive thing in this stage — and they can
    # only ever succeed on a dominant subject, so a photo without one skips them
    # entirely.
    h, w = img.shape[:2]
    if (pet_be is None or subject_share < cfg.orientation_min_subject
            or w <= h):
        return 0, 0.0, ""
    upright, _ = _best_person(pet_be, img)
    best_deg, best_score, best_share = 0, upright, 0.0
    for deg in (90, 270):
        score, share = _best_person(pet_be, rotate_image(img, deg))
        if score > best_score:
            best_deg, best_score, best_share = deg, score, share
    if (best_deg and best_score >= cfg.orientation_person_min
            and best_score - upright >= cfg.orientation_person_margin
            and best_share >= cfg.orientation_min_subject
            and best_score >= animal_score):
        return best_deg, best_score, "person"
    return 0, 0.0, ""


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
            rotate, conf, orient_src = 0, 0.0, ""
            face_report = face_backend.DetectionReport()
            try:
                img, scale = _load_bgr(str(path), cfg.detect_max_side)
                found = _detect_on(img, scale, cfg, face_be, pet_be)

                # -- is this photo stored sideways? --------------------------
                # Someone/something is here but no face resolved: that is the
                # signature of a photo whose pixels are turned. Probing costs
                # three score-only SCRFD passes and is skipped for the vast
                # majority of images, which answer on the first pass.
                # SCRFD finding nothing at all is the signal, not the
                # cross-check's verdict: a face it did find and an animal then
                # claimed says the photo was readable, so leave it be.
                if (face_be is not None and not found.report.faces
                        and (found.animals or found.humans)):
                    rotate, conf, orient_src = _resolve_rotation(
                        img, pet_be, cfg, found.max_subject_share,
                        found.animal_score)
                    if rotate:
                        img = rotate_image(img, rotate)
                        found = _detect_on(img, scale, cfg, face_be, pet_be)
                        stats.rotated += 1

                face_report = found.report
                human_faces = found.faces
                animals, humans = found.animals, found.humans
                stats.human_animals_dropped += found.human_animals
                stats.nonhuman_suppressed += found.suppressed_faces

                # -- rewrite this file's detections as a unit ----------------
                conn.execute("DELETE FROM faces WHERE file_id=?", (fid,))
                conn.execute("DELETE FROM animal_detections WHERE file_id=?", (fid,))
                conn.execute("DELETE FROM nonhuman_detections WHERE file_id=?", (fid,))
                conn.execute("DELETE FROM orientation WHERE file_id=?", (fid,))
                if rotate:
                    conn.execute(
                        """INSERT INTO orientation
                           (file_id, rotate_deg, source, confidence, created_at)
                           VALUES (?,?,?,?,?)""",
                        (fid, rotate, orient_src, conf, now))
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
