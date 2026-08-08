"""One entry point for reading a file, dispatching on what it is.

Everything above this module -- the service that stores an outcome, the runner
that schedules the pass -- goes through ``read``. That is deliberate: whether a
PDF's text layer was worth anything, and therefore whether the file still needs
reading off its pixels, is one decision, and it belongs in one place rather than
being re-derived by each caller from a block count.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import ocr, office, pdf, plain, raster
from .results import (
    DOCUMENTS,
    NO_TEXT_LAYER,
    OCR,
    OFFICE,
    OPENDOCUMENT,
    PDF_OCR,
    PDF_TEXT,
    PLAIN,
    TOO_LARGE,
    UNSUPPORTED,
    Block,
    Extraction,
)

# Every extension the document-text half can read, whatever the outcome. A file
# outside this set never enters the backlog, so it is never counted as work and
# never gets a row saying it was skipped -- the same way an audio file is simply
# not a candidate for face detection.
DOCUMENT_READABLE = frozenset({"pdf"}) | office.OFFICE_EXTS | plain.PLAIN_EXTS

# Read but never successfully: reported per file so the panel's claim about
# legacy formats is visible where the file is, not only in the documentation.
DOCUMENT_REFUSED = office.LEGACY_EXTS

# Pictures the OCR half will open. Deliberately not every image extension the
# catalogue knows: raw camera formats are photographs of the world by
# definition, cost the most to decode, and are the least likely thing anyone
# ever photographed a document with.
IMAGE_READABLE = frozenset(
    {"jpg", "jpeg", "png", "webp", "bmp", "tif", "tiff", "heic", "heif", "jfif", "gif"}
)


@dataclass(frozen=True)
class Limits:
    """Every bound one file is read under, in one place.

    Passed down rather than read from config here: ``trove/text`` is L1 and
    knows nothing about an archive's settings, so the service layer resolves
    these and hands them over.
    """

    max_bytes: int = 64 * 1024 * 1024
    # Pages beyond this and the file is skipped rather than read. A 2,000-page
    # scanned book is an hour of OCR on its own, and one file should never be
    # able to hold the stage that long.
    max_pages: int = 200
    render_dpi: int = 200
    detect_side: int = 736
    # Below this many characters a page carries no useful text layer.
    min_chars_per_page: int = 40
    # ...and above this much image coverage it is a picture of a page rather
    # than a page with a picture on it. Both are required -- see
    # ``pdf.looks_scanned`` for why sparse text alone is not evidence.
    min_image_cover: float = 0.5
    # Where the OCR weights live, since they are downloaded now rather than
    # carried inside the package (ADR 0019). It rides here for the same reason
    # everything else does: this layer knows nothing about the installation, and
    # the alternative -- reading a Config from inside a reader -- is the import
    # this module is arranged to avoid. Empty is the no-OCR default, which is
    # sound because nothing reaches the engine without ``OCR in extractors``.
    models_dir: str = ""


def available(extractors: frozenset[str]) -> bool:
    """Whether this build can run at least one of the halves it was asked for.

    Reading document text is available on any build: everything except PDF is
    standard library, so a missing ``pypdfium2`` costs PDFs a per-file skip
    rather than the whole feature -- the shape a missing ffmpeg has for videos.

    Reading picture text is different, because there is no partial state to
    degrade to. Its models live inside its package, so the engine either imports
    with everything it needs or is absent entirely.

    Either half being runnable is enough for the stage, since they share it.
    """
    if DOCUMENTS in extractors:
        return True
    return OCR in extractors and ocr.available()


def readable_exts(wanted: frozenset[str]) -> frozenset[str]:
    """Every extension the switched-on halves consider work.

    The backlog query is built from this rather than from ``media_type``, so a
    file the pass would only skip never enters it at all -- a .zip is not
    counted as queued work and never gets a row explaining that it is not a
    document. Refused formats are included on purpose: they enter once, get a
    row saying why, and then stop counting.
    """
    exts: frozenset[str] = frozenset()
    if DOCUMENTS in wanted:
        exts |= DOCUMENT_READABLE | DOCUMENT_REFUSED
    if OCR in wanted:
        # Every picture, plus PDFs -- a scan is a PDF whose pages are pictures,
        # and which of its pages need reading that way is decided per page.
        exts |= IMAGE_READABLE | {"pdf"}
    return exts


def eligible(ext: str, media_type: str, wanted: frozenset[str]) -> bool:
    """Whether this file is work for the text pass, given the halves switched on."""
    if media_type == "document":
        return (DOCUMENTS in wanted or OCR in wanted) and ext in readable_exts(wanted)
    if media_type == "image":
        return OCR in wanted and ext in IMAGE_READABLE
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


def _read_pdf(path: Path, wanted: frozenset[str], limits: Limits) -> Extraction:
    """One PDF, read by whichever halves are on and whichever each page needs.

    **This is why the two features share a stage.** Whether a page needs reading
    as pictures cannot be known until its text layer has been tried, so the file
    is opened once and each page is routed on what that open found. A forty-page
    contract with a scanned appendix gets both treatments, in page order, and
    comes out as one document.
    """
    if not pdf.available():
        raise ValueError(f"{UNSUPPORTED} pdf: no PDF reader is installed")
    stats = pdf.page_stats(path)
    if len(stats) > limits.max_pages:
        raise ValueError(
            f"{TOO_LARGE} the {limits.max_pages}-page limit for reading ({len(stats)} pages)"
        )

    layer = {block.page: block for block in pdf.read(path)[0]} if DOCUMENTS in wanted else {}
    scanned = [
        s for s in stats if pdf.looks_scanned(s, limits.min_chars_per_page, limits.min_image_cover)
    ]
    # Document text alone, and nothing came out of the text layer. If the pages
    # look like pictures, say so specifically -- that reason is what explains the
    # file to its owner and what makes switching the picture half on come back
    # for it.
    if not layer and OCR not in wanted:
        if scanned:
            raise ValueError(f"{NO_TEXT_LAYER}: this PDF is pictures of text, not text")
        raise ValueError(f"{NO_TEXT_LAYER}: the file holds no readable text")

    blocks: list[Block] = []
    confidences: list[float] = []
    read_pages = {s.number for s in scanned} if OCR in wanted else set()
    for stat in stats:
        if stat.number in layer:
            blocks.append(layer[stat.number])
        elif stat.number in read_pages:
            lines, confidence = ocr.read_array(
                raster.pdf_page(path, stat.number, limits.render_dpi),
                limits.models_dir,
                detect_side=limits.detect_side,
            )
            if lines:
                blocks.append(Block(stat.number, "\n".join(lines)))
                if confidence is not None:
                    confidences.append(confidence)
    if not blocks:
        raise ValueError(f"{NO_TEXT_LAYER}: nothing could be read from this PDF")

    # `pdf-ocr` only where pixels actually contributed, so the extractor on the
    # row says how the text was obtained rather than which halves were enabled.
    extractor = PDF_OCR if confidences else PDF_TEXT
    mean = sum(confidences) / len(confidences) if confidences else None
    return Extraction(extractor, tuple(blocks), pages=len(stats), confidence=mean)


def _read_document(path: Path, ext: str, wanted: frozenset[str], limits: Limits) -> Extraction:
    """One file that is expected to carry its own text."""
    if ext in DOCUMENT_REFUSED:
        raise ValueError(
            f"{UNSUPPORTED} legacy Office format (.{ext}): no pure-Python reader exists"
        )
    if ext == "pdf":
        return _read_pdf(path, wanted, limits)
    reader = office.read if ext in office.OFFICE_EXTS else plain.read
    blocks = reader(path, ext)
    if not blocks:
        raise ValueError(f"{NO_TEXT_LAYER}: the file holds no readable text")
    return Extraction(_document_extractor(ext), tuple(blocks))


def _read_picture(path: Path, limits: Limits) -> Extraction:
    """One photograph or screenshot.

    Most pictures hold no writing, and that is a skip rather than a failure --
    the reason says so, the file stops being pending, and nothing suggests
    anything went wrong.
    """
    found = ocr.read_image(path, limits.models_dir, detect_side=limits.detect_side)
    if found is None:
        raise ValueError(f"{NO_TEXT_LAYER}: no writing was found in this picture")
    return found


def read(
    path: Path,
    ext: str,
    media_type: str,
    wanted: frozenset[str],
    *,
    limits: Limits | None = None,
) -> Extraction:
    """Read one file, or raise ``ValueError`` carrying the reason it was not.

    Every bound lives on ``limits`` rather than being checked by the caller, so
    that one pathological input costs a skip and not the pass, whichever reader
    it reaches.
    """
    bounds = limits or Limits()
    size = path.stat().st_size
    if size > bounds.max_bytes:
        raise ValueError(
            f"{TOO_LARGE} the {bounds.max_bytes:,}-byte limit for reading ({size:,} bytes)"
        )
    if media_type == "image":
        if OCR not in wanted or ext not in IMAGE_READABLE:
            raise ValueError(f"{UNSUPPORTED} .{ext}: nothing switched on reads pictures")
        return _read_picture(path, bounds)
    if ext in DOCUMENT_READABLE or ext in DOCUMENT_REFUSED:
        if DOCUMENTS not in wanted and not (ext == "pdf" and OCR in wanted):
            raise ValueError(f"{UNSUPPORTED} .{ext}: nothing switched on can read this")
        return _read_document(path, ext, wanted, bounds)
    raise ValueError(f"{UNSUPPORTED} .{ext}: nothing switched on can read this")
