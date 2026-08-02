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
  People -- but *recorded*, in ``nonhuman_detections``, so the drop stays
  reviewable. The detector is guessing here, and a guess a user cannot overrule
  is the wrong kind of confident (see detect/persist.py).

That replaces the old one-directional rule, which dropped any face merely
*contained* in an animal box — so a bogus full-frame "dog" over a reclining
person deleted the real face from People *and* kept the phantom pet.

The same signal gives the stage its other job: **resolving true orientation**.
EXIF is applied on decode and settles most photos, but this archive is full of
re-exports whose pixels are turned while their orientation tag says they are
not, and on those every model here fails — SCRFD finds no face at all and the
person becomes a ``dog``. A quorum of faces — or a ``person`` box — that resolves
at a quarter turn and not upright says which way up the photo really is; it is
recorded in ``orientation`` and detection is redone there, so the boxes and the
app's view of the photo are both upright. Deliberately narrow (see
``_resolve_rotation``): turning a correctly stored photo over is worse than
leaving a sideways one alone.

Resumable and incremental, like the stages it replaces: an image is pending when
it lacks a current ``face_scan`` OR a current ``pet_scan`` row, and is processed
as a unit: both detectors run, and every detection row the file has is rewritten
together (``detect/persist.py``), so the cross-check is always consistent.
Read-only over originals. Clustering into people/pets is a separate step
(faces/pets cluster.py).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from .. import thumbnails
from ..config import Config
from ..db import database as db
from ..faces import backend as face_backend
from ..faces import fiqa
from ..pets import backend as pet_backend
from ..pets.extract import scan_source as pet_scan_source
from .persist import rewrite_file_detections

logger = logging.getLogger(__name__)


@dataclass
class DetectStats:
    processed: int = 0  # files examined this run (images + videos)
    videos: int = 0  # of which were videos
    video_frames: int = 0  # sampled keyframes actually decoded
    faces_found: int = 0
    images_with_faces: int = 0
    animals: int = 0
    photos_with_animals: int = 0
    nonhuman_suppressed: int = 0  # faces dropped as an animal's own face
    human_animals_dropped: int = 0  # "pets" that a person box exposed as people
    rotated: int = 0  # photos found to be stored sideways
    candidates: int = 0
    rejected_score: int = 0
    rejected_size: int = 0
    rejected_clipped: int = 0
    rejected_nonhuman: int = 0
    errors: int = 0
    error_samples: list = field(default_factory=list)


# hidden=0 skips non-canonical duplicate copies (dedup runs before detection);
# re-detecting a duplicate is pure waste — the canonical copy is scanned instead.
#
# Videos only join the pending population when detect_video_frames > 0 --
# otherwise a video would sit "pending" forever with the feature turned off,
# and the stage would never report itself as up to date.
def _media_types(cfg: Config) -> str:
    return "('image','video')" if cfg.detect_video_frames > 0 else "('image')"


def _pending_where(cfg: Config) -> str:
    return f"""
        f.present=1 AND f.media_type IN {_media_types(cfg)} AND f.hidden=0
        AND (fs.file_id IS NULL
             OR ps.file_id IS NULL
             OR ps.source_sha256 IS NOT f.sha256
             OR ps.model_source IS NOT :pet_src)
    """


def image_count(conn, cfg: Config | None = None, root_id: int | None = None) -> int:
    """Total present, canonical (non-duplicate) media the stage counts.

    ``cfg`` is optional so old call sites keep working, but pass it: without
    it this always counts images only, which under-counts the population
    relative to ``pending_count`` while video detection is enabled and makes
    "done / total" progress look wrong.
    """
    media_types = _media_types(cfg) if cfg is not None else "('image')"
    rc = " AND f.root_id=?" if root_id is not None else ""
    params = (root_id,) if root_id is not None else ()
    return conn.execute(
        f"""SELECT COUNT(*) FROM files f
            WHERE f.present=1 AND f.media_type IN {media_types} AND f.hidden=0{rc}""",
        params,
    ).fetchone()[0]


def pending_count(conn, cfg: Config, root_id: int | None = None) -> int:
    """Present canonical media missing a current face OR pet scan."""
    rc = " AND f.root_id=:root" if root_id is not None else ""
    p = {"pet_src": pet_scan_source(cfg)}
    if root_id is not None:
        p["root"] = root_id
    return conn.execute(
        f"""SELECT COUNT(*) FROM files f
            LEFT JOIN face_scan fs ON fs.file_id=f.id
            LEFT JOIN pet_scan ps ON ps.file_id=f.id
            WHERE {_pending_where(cfg)}{rc}""",
        p,
    ).fetchone()[0]


def _pending(conn, cfg: Config, batch_size: int):
    return conn.execute(
        f"""SELECT f.id, f.rel_path, f.sha256, f.media_type, r.path AS root_path,
                   mm.duration_s, (fs.file_id IS NULL) AS need_face
            FROM files f JOIN roots r ON r.id=f.root_id
            LEFT JOIN face_scan fs ON fs.file_id=f.id
            LEFT JOIN pet_scan ps ON ps.file_id=f.id
            LEFT JOIN media_meta mm ON mm.file_id=f.id
            WHERE {_pending_where(cfg)}
            ORDER BY f.id
            LIMIT :lim""",
        {"pet_src": pet_scan_source(cfg), "lim": batch_size},
    ).fetchall()


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
        try:
            face_be = face_backend.FaceBackend(
                cfg.cache_dir,
                min_score=cfg.faces_min_score,
                min_px=cfg.faces_min_px,
                max_side=cfg.faces_max_side,
                det_size=cfg.faces_det_size,
                max_clipped_fraction=cfg.faces_max_clipped_fraction,
                min_focus=cfg.faces_min_focus,
                max_extreme_fraction=cfg.faces_max_extreme_fraction,
                quality_version=cfg.faces_quality_version,
                log=log,
            )
        except Exception as e:
            # Weights that could not be fetched or loaded must not take the other
            # detector down with them: report and carry on pets-only.
            if log:
                log(f"people detection unavailable: {e}")
            logger.warning("people detection unavailable", exc_info=True)
    if pet_backend.available():
        try:
            pet_be = pet_backend.PetBackend(
                cfg.cache_dir,
                min_score=cfg.pets_min_score,
                min_px=cfg.pets_min_px,
                max_side=cfg.pets_max_side,
                species=cfg.pets_species,
                human_min_score=cfg.pets_human_min_score,
                model_source=pet_scan_source(cfg),
                log=log,
            )
        except Exception as e:
            if log:
                log(f"pet detection unavailable: {e}")
            logger.warning("pet detection unavailable", exc_info=True)
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
        # pillow-heif is optional (HEIC support only) and wraps a native libheif
        # binding, so a broken or partial install fails in more ways than
        # ImportError -- a missing shared library surfaces as OSError. Broad on
        # purpose: without HEIC support these files simply fail to decode below
        # like any other unreadable file, which must never be fatal. Silent by
        # design too: this runs per image decoded, so a log line here would be
        # one per file, ~150k of them.
        pass
    with Image.open(path) as im:
        orig_side = max(im.size)
        try:
            im.draft("RGB", (max_side, max_side))
        except Exception:
            # draft() is a decode-speed optimisation implemented only for a
            # few formats; failing (or being a no-op) for the rest is normal,
            # not an error -- the full decode below still succeeds. Silent by
            # design: this runs per image decoded, so logging here would be a
            # line per file, ~150k of them, for a non-event.
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
        if any(_iou(a, p) >= min_iou and (not outscore or p.score >= a.score) for p in humans):
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

    faces: list = field(default_factory=list)  # kept, human
    animals: list = field(default_factory=list)  # kept, real animals
    humans: list = field(default_factory=list)  # person boxes (context)
    report: object = None  # the raw DetectionReport
    human_animals: int = 0  # pets that were really people
    # (face, index into `animals`) for each face the veto dropped. Kept, not
    # just counted: they are written to nonhuman_detections so the veto stays
    # reviewable -- see detect/persist.py::_save_suppressed.
    suppressed: list = field(default_factory=list)
    max_subject_share: float = 0.0  # biggest box, as a frame share
    animal_score: float = 0.0  # best animal reading, pre-veto


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
        found.max_subject_share = max((b.w * b.h for b in (*animals, *humans)), default=0) / max(
            1, w * h
        )
        animals, human_like = _drop_human_animals(animals, humans, cfg.pets_human_iou)
        if animals:
            # Survivors may still be people who are simply not vertical here —
            # the quarter-turns are where YOLOX reads them as `person` again.
            turn_humans = _human_boxes_on_turns(img, pet_be)
            humans += turn_humans
            animals, turned = _drop_human_animals(
                animals, turn_humans, cfg.pets_human_iou, outscore=True
            )
            human_like += turned
        found.human_animals = len(human_like)
        for box in (*animals, *humans):  # -> this frame's full-res pixels
            box.x = max(0, round(box.x * inv))
            box.y = max(0, round(box.y * inv))
            box.w = round(box.w * inv)
            box.h = round(box.h * inv)
        found.animals, found.humans = animals, humans

    if face_be is not None:
        found.report = face_be.detect_report(img, scale)

    for fc in found.report.faces:
        vetoed_by = next(
            (
                i
                for i, a in enumerate(found.animals)
                if _overlap_fraction(fc.x, fc.y, fc.w, fc.h, a.x, a.y, a.w, a.h)
                >= cfg.pets_face_overlap
            ),
            None,
        )
        in_person = any(
            _overlap_fraction(fc.x, fc.y, fc.w, fc.h, p.x, p.y, p.w, p.h) >= cfg.pets_face_overlap
            for p in found.humans
        )
        if vetoed_by is not None and not in_person:
            found.report.rejected["nonhuman"] = found.report.rejected.get("nonhuman", 0) + 1
            found.suppressed.append((fc, vetoed_by))
            continue
        found.faces.append(fc)
    return found


def _best_person(pet_be, img) -> tuple[float, float]:
    """``(confidence, share of the frame)`` of the most confident person box."""
    h, w = img.shape[:2]
    frame = max(1, w * h)
    best = max(pet_be.detect_humans(img), key=lambda p: p.score, default=None)
    return (best.score, best.w * best.h / frame) if best else (0.0, 0.0)


def _resolve_rotation(img, face_be, pet_be, cfg: Config, subject_share: float, animal_score: float):
    """Find the quarter turn that makes this photo's subjects upright.

    Two kinds of evidence, in order of how specific they are:

    **A quorum of faces.** Several faces that only resolve once the photo is
    turned is decisive — people do not all lie down in the same direction. This
    is what catches a sideways group photo, the case with the most to lose: one
    such photo in this archive shows five faces at 90 degrees and none at all as
    stored, so leaving it costs five people from People *and* leaves a phantom
    pet on top of them. A quorum is required because a *lone* rotated face is
    nearly always a doll, a cake figurine or someone lying down — that was
    measured on this archive, and single-face evidence is not used at all.

    **A person box.** A YOLOX ``person`` reading that appears at a quarter turn
    and not upright. That asymmetry is the signature of a sideways-stored photo
    of a person, and it is what the pet cross-check already exploits. It carries
    less information than a face quorum, so it is fenced in much more tightly
    (below).

    Being wrong here is expensive — turning a correctly stored photo over is
    worse than leaving a sideways one alone — so the person path's guards each
    earn their place against a real counterexample from this archive:

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

    A face quorum needs none of those: it is already saying "several people are
    upright this way", which no lying-down or close-up pet case can imitate.
    Both paths try the quarter turns only — pixels stored upside down are
    vanishingly rare, while detectors fire happily on upside-down subjects.

    Returns ``(degrees, confidence, source)``, or ``(0, 0.0, "")`` to leave the
    photo untouched.
    """
    # A quorum of faces first: cheaper than the person path (SCRFD without
    # ArcFace) and far more specific when it fires.
    if face_be is not None:
        best_deg, best = 0, []
        for deg in (90, 270):
            faces = [
                s for s in face_be.probe_faces(rotate_image(img, deg)) if s >= cfg.faces_min_score
            ]
            if len(faces) > len(best):
                best_deg, best = deg, faces
        if len(best) >= cfg.orientation_min_faces:
            return best_deg, max(best), "faces"

    # Three YOLOX passes — the most expensive thing in this stage — and they can
    # only ever succeed on a dominant subject in a landscape frame, so a photo
    # without one skips them entirely.
    h, w = img.shape[:2]
    if pet_be is None or subject_share < cfg.orientation_min_subject or w <= h:
        return 0, 0.0, ""
    upright, _ = _best_person(pet_be, img)
    best_deg, best_score, best_share = 0, upright, 0.0
    for deg in (90, 270):
        score, share = _best_person(pet_be, rotate_image(img, deg))
        if score > best_score:
            best_deg, best_score, best_share = deg, score, share
    if (
        best_deg
        and best_score >= cfg.orientation_person_min
        and best_score - upright >= cfg.orientation_person_margin
        and best_share >= cfg.orientation_min_subject
        and best_score >= animal_score
    ):
        return best_deg, best_score, "person"
    return 0, 0.0, ""


# -- video sampling ----------------------------------------------------------
# Frames sampled per video and where in the clip: evenly spread but pulled in
# from the very ends, where a title card or a black opening/closing frame sits
# more often than a moment that actually represents the video. Same idea as
# services/semantic.py's _VIDEO_FRAME_FRACTIONS (0.15, 0.5, 0.85 for 3 frames),
# generalized to any frame count with the same 0.15 margin on each end.
_VIDEO_FRAME_MARGIN = 0.15


def _video_frame_fractions(n: int, margin: float = _VIDEO_FRAME_MARGIN) -> tuple[float, ...]:
    if n <= 0:
        return ()
    if n == 1:
        return (0.5,)
    step = (1 - 2 * margin) / (n - 1)
    return tuple(margin + i * step for i in range(n))


def _format_offset(secs: float) -> str:
    secs = max(0.0, secs)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def _video_offsets(duration_s, n: int) -> list[str]:
    """ffmpeg ``-ss`` offsets for up to ``n`` frames spread across a video.

    Mirrors ``services.semantic._video_frame_offsets``: falls back to one fixed
    early offset when the duration is unknown (enrich hasn't run yet, or
    ffprobe couldn't read this container) rather than refusing to detect the
    video at all. Never returns a duplicate offset -- a very short clip whose
    fractions round to the same timestamp yields fewer, not repeated, frames.
    """
    if n <= 0:
        return []
    try:
        duration_s = float(duration_s)
    except (TypeError, ValueError):
        duration_s = 0.0
    if duration_s <= 0:
        return ["00:00:01"]
    offsets, seen = [], set()
    for frac in _video_frame_fractions(n):
        text = _format_offset(min(duration_s * frac, max(0.0, duration_s - 0.05)))
        if text not in seen:
            seen.add(text)
            offsets.append(text)
    return offsets


# -- per-video duplicate collapse ---------------------------------------------
# The same person or animal can be detected in several of a video's sampled
# frames. Left alone that would flood clustering with near-duplicate rows and
# inflate every count (faces_found, animals, "N people in this video" ...), so
# each surviving detection must be the ONE row for a given individual, kept
# from whichever frame gave the best-quality read of them.


def _best_face_quality(face) -> float:
    # quality_score is the aligned-crop focus/exposure measure (0-1); det_score
    # (`.score`) is the fallback when it isn't populated (defensive -- the real
    # Face dataclass always sets it, but keeps this helper usable on stand-ins).
    q = getattr(face, "quality_score", None)
    return q if q else face.score


def collapse_video_faces(entries: list[tuple], threshold: float) -> list[tuple]:
    """Collapse near-duplicate faces across a video's sampled frames.

    ``entries`` is ``[(Face, frame_offset), ...]`` in frame order. Each face is
    compared against the ones already kept (not pairwise against every other
    detection) by cosine similarity of their L2-normalized ArcFace embeddings
    -- cheap and order-independent enough for the handful of frames a video
    contributes; a video with hundreds of faces would want something smarter,
    but ``detect_video_frames`` is small by design. A similarity at/above
    ``threshold`` is the same person across frames; the one with the higher
    quality_score (falling back to det_score) is kept, so the surviving row's
    frame_offset is a frame the app can still re-derive a good crop from.
    """
    import numpy as np

    kept: list[list] = []  # [Face, offset], mutated in place as better hits arrive
    for face, offset in entries:
        best_i, best_sim = -1, -1.0
        for i, (kface, _koff) in enumerate(kept):
            sim = float(np.dot(face.embedding, kface.embedding))
            if sim > best_sim:
                best_sim, best_i = sim, i
        if best_i >= 0 and best_sim >= threshold:
            kface, _koff = kept[best_i]
            if _best_face_quality(face) > _best_face_quality(kface):
                kept[best_i] = [face, offset]
        else:
            kept.append([face, offset])
    return [(f, o) for f, o in kept]


def collapse_video_animals(entries: list[tuple], threshold: float) -> list[tuple]:
    """Same idea as ``collapse_video_faces``, for animal detections.

    Two boxes only ever collapse when they are additionally the same
    ``species`` -- a cat and a dog reading similarly on the DINOv2 embedding
    (rare, but possible for a low-detail crop) must never merge. The better of
    a pair is the higher ``det_score`` (animals have no separate quality
    metric).
    """
    import numpy as np

    kept: list[list] = []
    for animal, offset in entries:
        best_i, best_sim = -1, -1.0
        for i, (kanimal, _koff) in enumerate(kept):
            if kanimal.species != animal.species:
                continue
            sim = float(np.dot(animal.embedding, kanimal.embedding))
            if sim > best_sim:
                best_sim, best_i = sim, i
        if best_i >= 0 and best_sim >= threshold:
            kanimal, _koff = kept[best_i]
            if animal.score > kanimal.score:
                kept[best_i] = [animal, offset]
        else:
            kept.append([animal, offset])
    return [(a, o) for a, o in kept]


@dataclass
class _FileResult:
    """One decoded file's detections, shaped for persistence."""

    # [(Face|AnimalDetection, frame_offset|None), ...]; offset is
    # always None for a photo, and for a video after collapsing.
    face_hits: list = field(default_factory=list)
    animal_hits: list = field(default_factory=list)
    report: object = field(default_factory=face_backend.DetectionReport)
    human_animals: int = 0
    suppressed_hits: list = field(default_factory=list)  # (face, animal index)
    rotate: int = 0
    confidence: float = 0.0
    orient_source: str = ""


def _fix_sideways_photo(img, scale, found, cfg, face_be, pet_be):
    """Is this photo stored sideways?

    Someone/something is here but no face resolved: that is the signature of
    a photo whose pixels are turned. Probing costs three score-only SCRFD
    passes and is skipped for the vast majority of images, which answer on
    the first pass. SCRFD finding nothing at all is the signal, not the
    cross-check's verdict: a face it did find and an animal then claimed
    says the photo was readable, so leave it be.
    """
    rotate, conf, orient_src = 0, 0.0, ""
    if face_be is not None and not found.report.faces and (found.animals or found.humans):
        rotate, conf, orient_src = _resolve_rotation(
            img, face_be, pet_be, cfg, found.max_subject_share, found.animal_score
        )
        if rotate:
            img = rotate_image(img, rotate)
            found = _detect_on(img, scale, cfg, face_be, pet_be)
    return found, rotate, conf, orient_src


def _detect_photo(path, cfg, face_be, pet_be) -> _FileResult:
    """Decode one image and detect on it, self-correcting orientation if needed."""
    img, scale = _load_bgr(str(path), cfg.detect_max_side)
    found = _detect_on(img, scale, cfg, face_be, pet_be)
    found, rotate, conf, orient_src = _fix_sideways_photo(img, scale, found, cfg, face_be, pet_be)
    return _FileResult(
        face_hits=[(fc, None) for fc in found.faces],
        animal_hits=[(a, None) for a in found.animals],
        report=found.report,
        human_animals=found.human_animals,
        suppressed_hits=found.suppressed,
        rotate=rotate,
        confidence=conf,
        orient_source=orient_src,
    )


def _detect_video(path, cfg, cache_dir, row, face_be, pet_be, stats, commit_if_due) -> _FileResult:
    """Sample a video's frames, detect on each, and collapse repeats across them.

    ``stats`` is updated per frame directly (not returned and added by the
    caller), so a video that fails partway through keeps the frames it did
    finish -- matching what the inline loop this replaces did.
    """
    # ffmpeg already applies the container's rotation metadata
    # when it extracts a frame, so every sampled frame is
    # upright by construction: there is no rotation to resolve
    # and no `orientation` row to write for a video.
    fid = row["id"]
    result = _FileResult()
    offsets = _video_offsets(row["duration_s"], cfg.detect_video_frames)
    raw_faces, raw_animals = [], []
    for offset in offsets:
        frame_path = thumbnails.detect_frame_for(
            cache_dir,
            fid,
            path,
            offset,
            cfg.detect_video_frame_px,
            sha256=row["sha256"],
        )
        if frame_path is None:
            continue  # this offset alone failed; try the rest
        stats.video_frames += 1
        img, scale = _load_bgr(str(frame_path), cfg.detect_max_side)
        found = _detect_on(img, scale, cfg, face_be, pet_be)
        result.report.candidates += found.report.candidates
        for reason, n in found.report.rejected.items():
            result.report.rejected[reason] = result.report.rejected.get(reason, 0) + n
        stats.human_animals_dropped += found.human_animals
        stats.nonhuman_suppressed += len(found.suppressed)
        raw_faces.extend((fc, offset) for fc in found.faces)
        raw_animals.extend((a, offset) for a in found.animals)
        # A video costs one decode+detect PER sampled frame, so
        # the same time-budget relief extract() applies between
        # files is also applied between frames here -- otherwise a
        # single multi-frame video could itself hold the writer
        # past other connections' busy_timeout.
        commit_if_due()
    # Same person/animal across several frames must not become
    # several rows -- collapse before writing.
    result.face_hits = collapse_video_faces(raw_faces, cfg.detect_video_same_face)
    result.animal_hits = collapse_video_animals(raw_animals, cfg.detect_video_same_animal)
    # No frame at all (no ffmpeg, unreadable container) is a
    # clean permanent skip, not an error: _write_scan_markers
    # still writes them with zero counts, so this video
    # is never retried forever.
    return result


def _prepare_backends(conn, cfg: Config, progress, limit, face_be, pet_be):
    """Load whichever detectors weren't already provided, and size the run."""
    if not available():
        raise RuntimeError("detect backend unavailable (needs faces and/or pets)")
    if face_be is None and pet_be is None:
        face_be, pet_be = make_backends(
            cfg, log=(lambda m: progress.update(0, 0, m)) if progress else None
        )
        if face_be is None and pet_be is None:
            # Both sets of weights failed to load. Stop here rather than walk the
            # pending images and mark them scanned with nothing detected.
            raise RuntimeError(
                "detect backend unavailable: neither the face nor the pet models "
                "could be loaded (see the messages above)"
            )
    # The FIQA assessor is attached per run rather than baked into the backend:
    # it depends on the calibration row, which this very run may create (see
    # _maybe_bootstrap_fiqa), and the backend is often reused across chunked runs.
    if face_be is not None:
        face_be.assessor = fiqa.make_assessor(conn, cfg)

    total = pending_count(conn, cfg)
    if limit is not None:
        total = min(total, limit)
    if progress is not None:
        progress.total = total
    return face_be, pet_be


def _make_committer(conn):
    """Build a callable that commits ``conn`` at most once every 2 seconds.

    ``force=True`` commits unconditionally and resets the clock either way --
    used once per batch, where a commit is always wanted.
    """
    import time as _time

    last = _time.monotonic()

    def commit_if_due(force: bool = False):
        nonlocal last
        if force or (_time.monotonic() - last) >= 2:
            conn.commit()
            last = _time.monotonic()

    return commit_if_due


def _write_scan_markers(conn, fid, result: _FileResult, sha256, pet_src, now):
    # Both scan markers, always written together so the image is never
    # reprocessed for one detector while the other's row is stale.
    face_report = result.report
    conn.execute(
        """INSERT OR REPLACE INTO face_scan
           (file_id, n_faces, n_candidates, rejected_score, rejected_size,
            rejected_focus, rejected_exposure, rejected_clipped,
            rejected_nonhuman, scanned_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            fid,
            len(result.face_hits),
            face_report.candidates,
            face_report.rejected.get("score", 0),
            face_report.rejected.get("size", 0),
            0,
            0,
            face_report.rejected.get("clipped", 0),
            face_report.rejected.get("nonhuman", 0),
            now,
        ),
    )
    conn.execute(
        """INSERT OR REPLACE INTO pet_scan
           (file_id, n_animals, source_sha256, model_source, scanned_at)
           VALUES (?,?,?,?,?)""",
        (fid, len(result.animal_hits), sha256, pet_src, now),
    )


def _finish_row(stats: DetectStats, progress, result: _FileResult, path):
    """Roll one file's outcome into the run's stats and progress reporting."""
    n_faces = len(result.face_hits)
    n_animals = len(result.animal_hits)
    stats.candidates += result.report.candidates
    stats.rejected_score += result.report.rejected.get("score", 0)
    stats.rejected_size += result.report.rejected.get("size", 0)
    stats.rejected_clipped += result.report.rejected.get("clipped", 0)
    stats.rejected_nonhuman += result.report.rejected.get("nonhuman", 0)
    stats.processed += 1
    stats.faces_found += n_faces
    stats.animals += n_animals
    if n_faces:
        stats.images_with_faces += 1
    if n_animals:
        stats.photos_with_animals += 1
    if progress is not None:
        progress.update(stats.processed, 0, path.name)


def _maybe_bootstrap_fiqa(conn, cfg: Config, face_be, stats, progress):
    # Once enough feature norms exist, fix the FIQA calibration and tier the
    # backlog that was extracted before it. A no-op on every later batch
    # (one indexed lookup), so the archive is calibrated exactly once and a
    # face's tier never depends on which batch it happened to land in.
    if face_be is not None:
        before = fiqa.load_calibration(conn, cfg.faces_fiqa_model)
        if (
            before is None
            and fiqa.bootstrap_calibration(
                conn,
                cfg,
                log=(lambda m: progress.update(stats.processed, 0, m)) if progress else None,
            )
            is not None
        ):
            conn.commit()
            face_be.assessor = fiqa.make_assessor(conn, cfg)


def extract(
    conn,
    cfg: Config,
    *,
    progress=None,
    batch_size: int = 32,
    limit: int | None = None,
    face_be=None,
    pet_be=None,
    cache_dir: str | None = None,
) -> DetectStats:
    """Detect people + animals for pending images/videos in one decode each.

    ``limit`` caps files this run (chunked runs / testing); None = until drained.
    Pass already-loaded backends to reuse them across chunks. Either backend may be
    None (feature unavailable): the pass then does only the other, and with no pet
    backend there is simply no animal cross-check.

    ``cache_dir`` is where sampled video keyframes are extracted to and cached
    (see ``thumbnails.detect_frame_for``); it defaults to ``cfg.cache_dir`` for
    the CLI's single shared catalog, but the GUI's per-archive isolation means
    each archive has its OWN cache directory, so it must pass that archive's
    (``cfg.archive_cache_dir(root_id)``) explicitly rather than relying on the
    default.
    """
    cache_dir = cache_dir if cache_dir is not None else cfg.cache_dir
    stats = DetectStats()
    face_be, pet_be = _prepare_backends(conn, cfg, progress, limit, face_be, pet_be)
    now = db.now_iso()
    pet_src = pet_scan_source(cfg)
    commit_if_due = _make_committer(conn)

    while True:
        remaining = None if limit is None else max(0, limit - stats.processed)
        if remaining == 0:
            break
        rows = _pending(conn, cfg, batch_size if remaining is None else min(batch_size, remaining))
        if not rows:
            break
        for row in rows:
            fid = row["id"]
            path = Path(row["root_path"]) / row["rel_path"]
            is_video = row["media_type"] == "video"
            result = _FileResult()
            try:
                if is_video:
                    stats.videos += 1
                    result = _detect_video(
                        path, cfg, cache_dir, row, face_be, pet_be, stats, commit_if_due
                    )
                else:
                    result = _detect_photo(path, cfg, face_be, pet_be)
                    if result.rotate:
                        stats.rotated += 1
                    stats.human_animals_dropped += result.human_animals
                    stats.nonhuman_suppressed += len(result.suppressed_hits)
                fiqa_model = getattr(getattr(face_be, "assessor", None), "model", None)
                rewrite_file_detections(conn, fid, now, result, pet_src, fiqa_model, row["sha256"])
            except Exception as e:  # bad/corrupt file, unreadable, etc.
                stats.errors += 1
                if len(stats.error_samples) < 5:
                    stats.error_samples.append(f"{path.name}: {e}")
                # No exc_info: ~150k files means a traceback per bad file would
                # flood the log until rotation discards everything useful.
                logger.warning("detection failed for %s: %s", path.name, e)
                # Back to zero, because the scan markers below are written
                # whether or not this file succeeded. Before this function was
                # broken into steps, the counts they record were assigned at the
                # very end of the rewrite block, so a rewrite that failed partway
                # left them at zero -- keep that. Reporting the detected counts
                # for a file whose rows only partly landed would claim faces the
                # catalog does not have.
                result = _FileResult()
            _write_scan_markers(conn, fid, result, row["sha256"], pet_src, now)
            _finish_row(stats, progress, result, path)
            # Flush to DB by time, not by batch size: a full 32-image batch is
            # a full SCRFD+YOLOX decode plus up to three extra YOLOX passes
            # each, which can hold the single writer well past the 30s
            # busy_timeout other connections (the parallel semantic worker,
            # HTTP handler writes) wait on -- surfacing as a spurious
            # "database is locked". This also cuts worst-case work lost on a
            # kill from a full batch to about 2 seconds.
            commit_if_due()
        commit_if_due(force=True)
        _maybe_bootstrap_fiqa(conn, cfg, face_be, stats, progress)

    return stats
