"""One entry point for reading a file, dispatching on what it is.

Everything above this module -- the service that stores an outcome, the runner
that schedules the pass -- goes through ``read``. That is deliberate: whether a
PDF's text layer was worth anything, and therefore whether the file still needs
Text in images, is one decision, and it belongs in one place rather than being
re-derived by each caller from a block count.
"""

from __future__ import annotations

from pathlib import Path

from . import office, pdf, plain
from .results import (
    DOCUMENTS,
    NO_TEXT_LAYER,
    OFFICE,
    OPENDOCUMENT,
    PDF_TEXT,
    PLAIN,
    TOO_LARGE,
    UNSUPPORTED,
    Extraction,
)

# Every extension the Documents half can read, whatever the outcome. A file
# outside this set never enters the backlog, so it is never counted as work and
# never gets a row saying it was skipped -- the same way an audio file is simply
# not a candidate for face detection.
DOCUMENT_READABLE = frozenset({"pdf"}) | office.OFFICE_EXTS | plain.PLAIN_EXTS

# Read but never successfully: reported per file so the panel's claim about
# legacy formats is visible where the file is, not only in the documentation.
DOCUMENT_REFUSED = office.LEGACY_EXTS


def available(extractors: frozenset[str]) -> bool:
    """Whether this build can run the extractors it was asked for.

    True for Documents on any build. Everything except PDF is standard library,
    so a missing ``pypdfium2`` costs PDFs a per-file skip rather than costing the
    whole feature -- which is the same shape as a missing ffmpeg making videos
    unindexable without disabling search by description.
    """
    return DOCUMENTS in extractors


def eligible(ext: str, media_type: str, wanted: frozenset[str]) -> bool:
    """Whether this file is work for the text pass, given the halves switched on."""
    if DOCUMENTS in wanted and media_type == "document":
        return ext in DOCUMENT_READABLE or ext in DOCUMENT_REFUSED
    return False


def _document_extractor(ext: str) -> str:
    """Which extractor name goes on the row, from the extension alone."""
    if ext == "pdf":
        return PDF_TEXT
    if ext in office.OOXML_EXTS:
        return OFFICE
    if ext in office.ODF_EXTS:
        return OPENDOCUMENT
    return PLAIN


def _read_document(path: Path, ext: str) -> Extraction:
    """One file that is expected to carry its own text."""
    if ext in DOCUMENT_REFUSED:
        raise ValueError(
            f"{UNSUPPORTED} legacy Office format (.{ext}): no pure-Python reader exists"
        )
    if ext == "pdf":
        if not pdf.available():
            raise ValueError(f"{UNSUPPORTED} pdf: no PDF reader is installed")
        blocks, pages = pdf.read(path)
        if not blocks:
            # Not a failure, and specifically not permanent: this is what a scan
            # looks like, and Text in images is the reader for it. Saying so in
            # the reason is how the file explains itself on the Documents card.
            raise ValueError(f"{NO_TEXT_LAYER}: this PDF is pictures of text, not text")
        return Extraction(PDF_TEXT, tuple(blocks), pages=pages)
    reader = office.read if ext in office.OFFICE_EXTS else plain.read
    blocks = reader(path, ext)
    if not blocks:
        raise ValueError(f"{NO_TEXT_LAYER}: the file holds no readable text")
    return Extraction(_document_extractor(ext), tuple(blocks))


def read(path: Path, ext: str, wanted: frozenset[str], *, max_bytes: int) -> Extraction:
    """Read one file, or raise ``ValueError`` carrying the reason it was not.

    ``max_bytes`` bounds a single file, because one pathological input should
    cost a skip rather than the pass. It is checked here rather than by the
    caller so that every reader inherits it.
    """
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"{TOO_LARGE} the {max_bytes:,}-byte limit for reading ({size:,} bytes)")
    if DOCUMENTS in wanted and (ext in DOCUMENT_READABLE or ext in DOCUMENT_REFUSED):
        return _read_document(path, ext)
    raise ValueError(f"{UNSUPPORTED} .{ext}: nothing switched on can read this")
