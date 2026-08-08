"""Reading text out of pixels, at two resolutions.

RapidOCR drives PaddleOCR's PP-OCRv6 detection and recognition models on the
onnxruntime this app already ships. The weights come inside its wheel, so unlike
every other model here there is nothing to download, nothing to hash-verify at
runtime, and no half-installed state: the feature is wholly present or wholly
absent.

**Detection and recognition run at different resolutions, and that is the whole
design.** The obvious arrangement — one pass over the full-resolution image —
was measured on a 4-core machine and costs about 1.5 s for a photograph, almost
all of it detection. Detection is not the cheap half. It is the *only* half that
runs on the overwhelming majority of images, because most photographs contain no
text, and its cost scales with input size:

    longest side 1600px -> 1.51s      960px -> 0.62s
                 1200px -> 0.87s      736px -> 0.57s

So detection runs on a downscaled copy and recognition runs on crops taken from
the original. Small text stays readable, because what the recogniser sees is
full-resolution pixels; and a text-free photograph costs 0.59 s instead of 1.51 s.
Measured on a 2480x3508 scan with 34 px body text, the two paths return
byte-identical text.

That correction matters because the first version of this plan assumed detection
was an order of magnitude cheaper than recognition, and proposed a "cheap
detection gate" to skip photographs early. The gate saves nothing: detection is
the cost, and skipping recognition afterwards is skipping the small half.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from .results import IMAGE_OCR, Block, Extraction

logger = logging.getLogger(__name__)

# What a caller passes to watch a one-time model download. Spelled here rather
# than imported from ``model_manifest``, which this module reaches for only
# inside functions -- resolving a weight must not drag the manifest into a
# process that merely asked whether OCR is installed.
Log = Callable[[str], None]

# What the detector's input is scaled down to, longest side in pixels. Below
# ~736 the cost stops falling (512 measured no faster) while boxes start being
# missed, so this is the knee rather than a compromise.
DEFAULT_DETECT_SIDE = 736

# Recognitions scoring below this are dropped. PP-OCR reports a per-line
# confidence, and the low tail is where it invents words out of texture --
# wallpaper, foliage, fabric -- which is exactly the noise that would make a
# photo archive's index worthless.
MIN_LINE_CONFIDENCE = 0.5

# The three weights, by manifest name, and which of RapidOCR's three sessions
# each one feeds. They used to arrive inside the wheel; they are downloaded once
# now, like every other model here, and the cost of that move is written up in
# ADR 0019. The classifier is in this table despite nothing ever calling it:
# ``RapidOCR.__init__`` builds all three sessions whatever ``use_cls`` says, so
# a missing classifier is a constructor failure rather than a lost feature.
MODELS = {"Det": "ppocr_det", "Cls": "ppocr_cls", "Rec": "ppocr_rec"}

# One engine per cache directory rather than one per process. A process only
# ever has one, but keying it means a test that points at a temporary cache
# cannot be handed the engine an earlier test built somewhere else -- which
# would pass, silently reading through the wrong files.
_engines: dict[str, Any] = {}


def available() -> bool:
    """Whether the OCR engine imports.

    Importability only, as with every other backend: whether the *weights* are
    here is ``models_ready``. The two used to be one question, because the
    models shipped inside the package -- see ADR 0019 for why they no longer do.
    """
    try:
        import rapidocr  # noqa: F401

        return True
    except Exception:  # pragma: no cover - optional dep
        # Broad on purpose, as elsewhere: a half-installed native dependency
        # (shapely, pyclipper) fails in more ways than ImportError.
        logger.debug("rapidocr unavailable; reading pictures of text disabled", exc_info=True)
        return False


def models_ready(cache_dir: str) -> bool:
    """Whether all three weights are already on this machine. Never downloads."""
    from .. import model_manifest

    return all(model_manifest.present(name, cache_dir) is not None for name in MODELS.values())


def ensure_models(cache_dir: str, log: Log | None = None) -> None:
    """Fetch whichever of the three weights are not here yet.

    Smallest first, so the two seconds of classifier and the ten of detector
    are not spent behind the 21 MB recogniser when the download is going to
    fail anyway -- the same ordering, and the same reason, as the face
    backend's embedder-before-detector.
    """
    from .. import model_manifest

    for name in sorted(MODELS.values(), key=lambda n: model_manifest.entry(n)["size"]):
        model_manifest.ensure(name, cache_dir, log=log)


def engine(cache_dir: str) -> Any:
    """The process-wide OCR engine for this cache, built once.

    Three ONNX sessions live behind it, so one instance is shared rather than
    one per job -- the text stage is the only caller, but it can be restarted
    within a session and should not pay the load again.

    Every path is passed explicitly. Left to itself RapidOCR would resolve each
    model against its own package directory and *download the missing one from
    ModelScope*, which would put a second, unpinned download origin behind a
    feature whose weights this app already fetches and hash-verifies itself.
    Naming the files is what turns that off.
    """
    from .. import model_manifest

    engine = _engines.get(cache_dir)
    if engine is None:
        from rapidocr import RapidOCR

        params = {
            f"{section}.model_path": str(model_manifest.path(name, cache_dir))
            for section, name in MODELS.items()
        }
        engine = _engines[cache_dir] = RapidOCR(params=params)
    return engine


def _downscaled(array: Any, side: int) -> tuple[Any, float]:
    """A copy no larger than ``side`` on its longest edge, and the scale used."""
    import numpy as np
    from PIL import Image

    height, width = array.shape[:2]
    longest = max(height, width)
    if longest <= side:
        return array, 1.0
    scale = side / longest
    # Bilinear rather than Lanczos: this copy is only ever looked at by the
    # detector, which wants the same filter PaddleOCR resizes with, and a
    # sharper one would move box edges relative to what it was trained on.
    small = Image.fromarray(array).resize(
        (max(1, int(width * scale)), max(1, int(height * scale))),
        Image.Resampling.BILINEAR,
    )
    return np.asarray(small), scale


def read_array(
    array: Any, cache_dir: str, *, detect_side: int = DEFAULT_DETECT_SIDE
) -> tuple[list[str], float | None]:
    """Lines of text found in one image, and the mean confidence of the reading.

    The confidence is what lets a result be shown as *read from a picture*
    rather than quoted as though it were typed, so it is carried out of here
    rather than thrown away once the lines are joined.

    Returns ``([], None)`` for an image with no text in it, which is the
    overwhelmingly common case and not a failure.
    """
    from rapidocr.ch_ppocr_rec.typings import TextRecInput

    ocr = engine(cache_dir)
    small, scale = _downscaled(array, detect_side)
    detected = ocr.text_det(small)
    if detected.boxes is None or len(detected.boxes) == 0:
        return [], None

    # Boxes come back in the downscaled frame; recognition happens on crops of
    # the original, which is what keeps small text readable.
    boxes = detected.boxes if scale == 1.0 else detected.boxes / scale
    crops = ocr.crop_text_regions(array, boxes)
    if not crops:
        return [], None

    recognised = ocr.text_rec(TextRecInput(img=crops))
    texts = list(recognised.txts or [])
    scores = [float(s) for s in (recognised.scores or [])]

    kept = [
        (text, score)
        for text, score in zip(texts, scores or [1.0] * len(texts), strict=False)
        if text and text.strip() and score >= MIN_LINE_CONFIDENCE
    ]
    if not kept:
        return [], None
    mean = sum(score for _text, score in kept) / len(kept)
    return [text.strip() for text, _score in kept], float(mean)


def read_image(
    path: Any, cache_dir: str, *, detect_side: int = DEFAULT_DETECT_SIDE
) -> Extraction | None:
    """One photograph or screenshot, or None when it holds no text.

    None rather than an empty extraction because a photograph with no writing in
    it is the normal case, not a failure and not an empty document: the caller
    records it as a skip with that reason, so it stops being pending without
    ever suggesting something went wrong.
    """
    from . import raster

    lines, confidence = read_array(raster.image(path), cache_dir, detect_side=detect_side)
    if not lines:
        return None
    return Extraction(IMAGE_OCR, (Block(None, "\n".join(lines)),), confidence=confidence)
