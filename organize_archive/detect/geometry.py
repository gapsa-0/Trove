"""Box arithmetic and quarter-turns: the geometry both cross-checks are built on.

Split out of ``extract.py`` because none of it knows what a detector is. These
are pure functions over boxes and arrays, which is what makes the two rules
above them -- "an animal box that is really a person" and "which way up is this
photo" -- testable without loading an ONNX model.

The one thing to keep straight here is which *direction* a rotation goes.
``np.rot90`` counts counter-clockwise quarter turns; the app, EXIF and the
``orientation`` table all speak clockwise display degrees. ``_TURNS`` is the
only place the two meet, and ``_rotate_boxes_back`` is its inverse for boxes.
"""

from __future__ import annotations


def overlap_fraction(fx, fy, fw, fh, ax, ay, aw, ah) -> float:
    """Fraction of the FIRST box that lies inside the second."""
    left = max(fx, ax)
    top = max(fy, ay)
    right = min(fx + fw, ax + aw)
    bottom = min(fy + fh, ay + ah)
    inter = max(0, right - left) * max(0, bottom - top)
    return inter / max(1, fw * fh)


def iou(a, b) -> float:
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


def rotate_boxes_back(humans, k: int, w: int, h: int):
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


def human_boxes_on_turns(img, pet_be):
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
        found.extend(rotate_boxes_back(humans, k, w, h))
    return found


def drop_human_animals(animals, humans, min_iou: float, *, outscore=False):
    """Split animal boxes into (real animals, boxes that are really people).

    ``outscore`` additionally requires the person box to read at least as
    strongly as the animal it would overturn. Evidence found only by turning the
    image is worth less — a bird in flight or a cat curled up will read as a
    person from some angle — so a confident animal is left alone unless the
    person reading matches it.
    """
    kept, human_like = [], []
    for a in animals:
        if any(iou(a, p) >= min_iou and (not outscore or p.score >= a.score) for p in humans):
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
