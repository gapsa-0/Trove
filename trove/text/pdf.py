"""Reading a PDF's text layer, one block per page.

`pypdfium2` -- BSD-3-Clause/Apache-2.0, a prebuilt PDFium in the wheel, no
compiler and no system library. Chosen over `pypdf` because it also rasterises,
which is what lets Text in images read a scanned page without a second PDF
dependency, and over PyMuPDF because that is AGPL-3.0 and this repository is
MIT.

**A PDF is the one format where "no text" is a real answer rather than a
failure.** A scan carries pixels and no text layer, and the file is not broken --
it simply needs a different reader. That distinction is the caller's to make
(``extract.py``), and it matters: the reason recorded for such a file must not
read as permanent, or switching Text in images on later would never revisit it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from .results import UNSUPPORTED, Block


def available() -> bool:
    """Whether the PDF reader is installed.

    ``find_spec`` rather than an import: this is asked while resolving whether a
    stage may run, and importing PDFium's shared library to answer a question
    about availability would load several MB for archives holding no PDFs.
    Everything else the Documents feature reads is standard library, so a missing
    reader makes PDFs a per-file skip rather than the whole feature unavailable.
    """
    return importlib.util.find_spec("pypdfium2") is not None


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
