"""One decoded frame in, one cross-checked ``Found`` out.

This is the fused pass itself: decode once, run both detectors over that single
array, and let each arbitrate the other. Split out of ``extract.py`` because a
photo and a video's sampled keyframe reach it by completely different routes
(``extract.py`` and ``video.py``) and neither should own it -- and because
"what is in this frame" is the part worth reading on its own, separately from
the batching, committing and progress reporting around it.

Nothing here touches the database or knows a file id. The caller decides what
to do with the answer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..config import Config
from ..faces.backend import FaceBackend
from ..pets.backend import PetBackend
from .geometry import ScoredBox, drop_human_animals, human_boxes_on_turns, overlap_fraction
from .results import Found

if TYPE_CHECKING:
    import numpy as np


def load_bgr(path: str, max_side: int) -> tuple[np.ndarray, float]:
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
    # Two names, because they are two types: `opened` is the ImageFile the
    # decoder hands back (only it has draft()), `im` the plain Image every later
    # step works on.
    with Image.open(path) as opened:
        orig_side = max(opened.size)
        try:
            opened.draft("RGB", (max_side, max_side))
        except Exception:
            # draft() is a decode-speed optimisation implemented only for a
            # few formats; failing (or being a no-op) for the rest is normal,
            # not an error -- the full decode below still succeeds. Silent by
            # design: this runs per image decoded, so logging here would be a
            # line per file, ~150k of them, for a non-event.
            pass
        im = ImageOps.exif_transpose(opened).convert("RGB")
        w, h = im.size
        rs = min(1.0, max_side / max(w, h)) if max(w, h) else 1.0
        if rs < 1.0:
            im = im.resize((max(1, round(w * rs)), max(1, round(h * rs))))
        arr = np.asarray(im)
    scale = (max(arr.shape[:2]) / orig_side) if orig_side else 1.0
    return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR), scale


def _detect_animals(
    img: np.ndarray, inv: float, cfg: Config, pet_be: PetBackend, found: Found
) -> None:
    """Fill in ``found``'s animal/person boxes, with the human veto applied."""
    animals, humans = pet_be.detect_with_humans(img)
    # Kept from before the veto: how confidently the photo reads as an
    # animal the way it is stored, which the orientation check weighs
    # against a person reading from some other angle.
    found.animal_score = max((a.score for a in animals), default=0.0)
    h, w = img.shape[:2]
    # Animals and people are separate types with the same box; ScoredBox is the
    # shape both satisfy, and without it the mixed tuple flattens to `object`.
    subjects: tuple[ScoredBox, ...] = (*animals, *humans)
    found.max_subject_share = max((b.w * b.h for b in subjects), default=0) / max(1, w * h)
    animals, human_like = drop_human_animals(animals, humans, cfg.pets_human_iou)
    if animals:
        # Survivors may still be people who are simply not vertical here —
        # the quarter-turns are where YOLOX reads them as `person` again.
        turn_humans = human_boxes_on_turns(img, pet_be)
        humans += turn_humans
        animals, turned = drop_human_animals(
            animals, turn_humans, cfg.pets_human_iou, outscore=True
        )
        human_like += turned
    found.human_animals = len(human_like)
    detected: tuple[ScoredBox, ...] = (*animals, *humans)
    for box in detected:  # -> this frame's full-res pixels
        box.x = max(0, round(box.x * inv))
        box.y = max(0, round(box.y * inv))
        box.w = round(box.w * inv)
        box.h = round(box.h * inv)
    found.animals, found.humans = animals, humans


def _apply_animal_veto(cfg: Config, found: Found) -> None:
    """Sort detected faces into kept-human and suppressed-as-an-animal's.

    A face inside a person box is human and is never suppressed; a face inside
    an animal box with no person over it is an animal's face. The second case
    is recorded rather than discarded -- see ``results.Found.suppressed``.
    """
    for fc in found.report.faces:
        vetoed_by = next(
            (
                i
                for i, a in enumerate(found.animals)
                if overlap_fraction(fc.x, fc.y, fc.w, fc.h, a.x, a.y, a.w, a.h)
                >= cfg.pets_face_overlap
            ),
            None,
        )
        in_person = any(
            overlap_fraction(fc.x, fc.y, fc.w, fc.h, p.x, p.y, p.w, p.h) >= cfg.pets_face_overlap
            for p in found.humans
        )
        if vetoed_by is not None and not in_person:
            found.report.rejected["nonhuman"] = found.report.rejected.get("nonhuman", 0) + 1
            found.suppressed.append((fc, vetoed_by))
            continue
        found.faces.append(fc)


def detect_on(
    img: np.ndarray,
    scale: float,
    cfg: Config,
    face_be: FaceBackend | None,
    pet_be: PetBackend | None,
) -> Found:
    """Run both detectors over one frame and cross-check them.

    Boxes come out in the frame's own full-resolution pixels (``1/scale``), so
    a caller that rotated ``img`` first gets boxes in that rotated frame — which
    is exactly where the app draws them.
    """
    inv = 1.0 / scale if scale else 1.0
    found = Found()  # its own default is an empty DetectionReport

    if pet_be is not None:
        _detect_animals(img, inv, cfg, pet_be, found)
    if face_be is not None:
        found.report = face_be.detect_report(img, scale)

    _apply_animal_veto(cfg, found)
    return found
