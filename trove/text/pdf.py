"""Reading a PDF's text layer, one block per page.

`pypdfium2` -- BSD-3-Clause/Apache-2.0, a prebuilt PDFium in the wheel, no
compiler and no system library. Chosen over `pypdf` because it also rasterises,
which is what lets Pictures of text read a scanned page without a second PDF
dependency, and over PyMuPDF because that is AGPL-3.0 and this repository is
MIT.

**A PDF is the one format where "no text" is a real answer rather than a
failure.** A scan carries pixels and no text layer, and the file is not broken --
it simply needs a different reader. That distinction is the caller's to make
(``extract.py``), and it matters: the reason recorded for such a file must not
read as permanent, or switching Pictures of text on later would never revisit it.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path

from .results import UNSUPPORTED, Block

# FPDF_PAGEOBJ_IMAGE. Spelled out rather than imported from pypdfium2.raw so
# that reading a page's object list does not require the optional dependency
# to be importable at module scope.
_IMAGE_OBJECT = 3


def available() -> bool:
    """Whether the PDF reader is installed.

    ``find_spec`` rather than an import: this is asked while resolving whether a
    stage may run, and importing PDFium's shared library to answer a question
    about availability would load several MB for archives holding no PDFs.
    Everything else the Documents feature reads is standard library, so a missing
    reader makes PDFs a per-file skip rather than the whole feature unavailable.
    """
    return importlib.util.find_spec("pypdfium2") is not None


@dataclass(frozen=True)
class PageStat:
    """What one page carries, as far as deciding how to read it goes."""

    number: int
    chars: int
    # Fraction of the page's area covered by its largest image. A scan is one
    # picture over the whole sheet; a photograph beside a paragraph is not.
    image_cover: float


def page_stats(path: Path) -> list[PageStat]:
    """Per-page character count and largest-image coverage.

    Cheap: it walks the page's object list and its text layer, and rasterises
    nothing. That is what lets the verdict below be taken for every page of
    every PDF without paying render costs for the ones that do not need it.
    """
    import pypdfium2 as pdfium

    try:
        doc = pdfium.PdfDocument(path)
    except pdfium.PdfiumError as exc:
        raise ValueError(f"{UNSUPPORTED} pdf: {exc}") from exc
    stats: list[PageStat] = []
    try:
        for number, page in enumerate(doc, start=1):
            try:
                width, height = page.get_size()
                area = float(width) * float(height) or 1.0
                cover = 0.0
                for obj in page.get_objects():
                    if obj.type != _IMAGE_OBJECT:
                        continue
                    left, bottom, right, top = obj.get_bounds()
                    cover = max(cover, abs(right - left) * abs(top - bottom) / area)
                textpage = page.get_textpage()
                try:
                    chars = len(textpage.get_text_range().strip())
                finally:
                    textpage.close()
                stats.append(PageStat(number, chars, min(cover, 1.0)))
            finally:
                page.close()
        return stats
    finally:
        doc.close()


def looks_scanned(stat: PageStat, min_chars: int, min_cover: float) -> bool:
    """Whether this page is a picture of text rather than text.

    **Both conditions, and the second one is the important one.** Sparse text
    alone is not evidence: a title page, a section divider, a page holding one
    table or one signature all have almost no extractable characters and nothing
    for OCR to add. Requiring a large image as well is what keeps the expensive
    half off them -- and on a long document those pages are common enough that
    treating them as scans would be most of a wasted run.
    """
    return stat.chars < min_chars and stat.image_cover >= min_cover


def read(path: Path) -> tuple[list[Block], int]:
    """A PDF's text, one block per page, and its page count.

    Pages with no extractable text are left out of the blocks but still counted,
    so a partly-scanned document reports honestly: three blocks out of forty
    pages is what a text layer covering only the cover sheet looks like, and the
    page numbers on the blocks that exist stay correct.

    Raises ``ValueError`` with an ``unsupported`` prefix for a file PDFium will
    not open at all -- encrypted, truncated, not really a PDF. Those are
    permanent for this reader, and rightly stop being retried.
    """
    import pypdfium2 as pdfium

    try:
        doc = pdfium.PdfDocument(path)
    except pdfium.PdfiumError as exc:
        raise ValueError(f"{UNSUPPORTED} pdf: {exc}") from exc

    blocks: list[Block] = []
    try:
        for number, page in enumerate(doc, start=1):
            textpage = page.get_textpage()
            try:
                body = textpage.get_text_range().strip()
            finally:
                textpage.close()
                page.close()
            if body:
                blocks.append(Block(number, body))
        return blocks, len(doc)
    finally:
        doc.close()
