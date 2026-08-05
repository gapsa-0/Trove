"""Turning a page or a picture into the pixels the reader looks at.

Two sources, one shape: an RGB uint8 array. A PDF page is rendered by the same
PDFium that read its text layer, and a photograph is decoded by Pillow.

**Not the cached thumbnail.** Every other stage that looks at pixels reads the
320 px thumbnail the app already has, which is what makes them cheap. Text at
320 px is gone, so this is the one stage that has to open the original — and
that, rather than the model, is most of what it costs.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from .results import TOO_LARGE, UNSUPPORTED

# A page is rendered at this many dots per inch before it is read. 200 is enough
# for body text at any normal size -- a 10pt line comes out about 28 px tall,
# where recognition wants ~20 -- and doubles to 400 the memory of 300.
DEFAULT_DPI = 200

# PDFium works in points (1/72 inch), so this is the multiplier its renderer
# takes rather than a resolution.
_POINTS_PER_INCH = 72.0

# Refuse to build an array larger than this many pixels. A malformed or hostile
# page can declare a size that turns a 200 dpi render into gigabytes, and the
# stage should lose one file rather than the machine.
MAX_PIXELS = 80_000_000


def available() -> bool:
    """Whether anything here can produce pixels.

    Pillow alone is enough for photographs; PDF pages additionally need
    pypdfium2, which ``pdf.available()`` answers for.
    """
    return importlib.util.find_spec("PIL") is not None


def _checked(width: int, height: int, what: str) -> None:
    if width * height > MAX_PIXELS:
        raise ValueError(
            f"{TOO_LARGE} the {MAX_PIXELS:,}-pixel limit for reading ({what}: {width}x{height})"
        )


def image(path: Path) -> Any:
    """One photograph or screenshot as an RGB array, the right way up.

    EXIF orientation is applied here. A phone photograph of a receipt is
    routinely stored rotated with a tag saying so, and text read from the
    unrotated pixels is text read sideways -- which the recogniser returns as
    confident nonsense rather than as nothing.
    """
    import numpy as np
    from PIL import Image, ImageOps

    with Image.open(path) as img:
        _checked(img.width, img.height, path.name)
        # exif_transpose before convert: the tag is dropped by convert on some
        # formats, and applying it afterwards would silently do nothing.
        upright = ImageOps.exif_transpose(img)
        return np.asarray((upright or img).convert("RGB"))


def pdf_page(path: Path, page_number: int, dpi: int = DEFAULT_DPI) -> Any:
    """One PDF page rasterised to an RGB array. ``page_number`` is 1-based.

    Rendered by the same PDFium that read the text layer, so a document is
    opened by one library whichever half of the pass ends up reading it.
    """
    import numpy as np
    import pypdfium2 as pdfium

    try:
        doc = pdfium.PdfDocument(path)
    except pdfium.PdfiumError as exc:
        raise ValueError(f"{UNSUPPORTED} pdf: {exc}") from exc
    try:
        page = doc[page_number - 1]
        try:
            scale = dpi / _POINTS_PER_INCH
            width, height = page.get_size()
            _checked(int(width * scale), int(height * scale), f"{path.name} p.{page_number}")
            bitmap = page.render(scale=scale)
            try:
                return np.asarray(bitmap.to_pil().convert("RGB"))
            finally:
                bitmap.close()
        finally:
            page.close()
    finally:
        doc.close()
