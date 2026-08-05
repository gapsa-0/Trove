"""The vocabulary the text-reading feature shares, in one place.

Spelled out as plain strings and small frozen dataclasses so that every layer
above -- the service that stores an outcome, the runner that schedules the pass,
the feature catalogue that names it for the user -- agrees about what an
extractor is called and what a reading looks like, without any of them holding a
second copy of the answer.
"""

from __future__ import annotations

from dataclasses import dataclass

# The two halves of the fused text pass, matching ``features.Feature.extractor``.
# Named here rather than in features.py for the same reason detector names are:
# features.py is L0 and may not import this module, so the two spellings are
# checked against each other in tests/unit/test_features.py instead.
DOCUMENTS = "documents"
OCR = "ocr"
BOTH_EXTRACTORS = frozenset({DOCUMENTS, OCR})

# What produced a file's text, recorded on ``doc_text.extractor``. `pdf-ocr` is
# the mixed case: a PDF whose text layer covered some pages and whose remaining
# pages were read as pictures.
PDF_TEXT = "pdf-text"
PDF_OCR = "pdf-ocr"
OFFICE = "office"
OPENDOCUMENT = "opendocument"
PLAIN = "plain"
IMAGE_OCR = "ocr"

# **A skip reason is a contract, not a message**, and it is worth being exact
# about what it decides. ``services/documents.py`` reads the prefix to choose
# between recording a file as *skipped* and recording it as an *error*. It does
# NOT decide whether the file is read again: an outcome row is written either
# way, carrying the sha256 and feature set it was produced under, and the
# four-legged predicate in ``pending_rows`` is the only thing that re-queues
# anything.
#
# So the distinction is "is something wrong here" -- an error is worth a user's
# attention and a skip is not. A scan with no text layer is not a failure; it is
# a file this half of the pass has nothing to say about, and switching Text in
# images on will bring it back through the ``wanted`` leg regardless of how it
# was labelled.
UNSUPPORTED = "unsupported"
TOO_LARGE = "media exceeds"
NO_TEXT_LAYER = "no text layer"

CLEAN_SKIP_PREFIXES = (UNSUPPORTED, TOO_LARGE, NO_TEXT_LAYER)


@dataclass(frozen=True)
class Block:
    """One run of text and the page it was read from.

    ``page`` is 1-based, and None for the formats that genuinely have no pages
    (a .txt, a .csv, a spreadsheet). It is not a guess: a chunk built from blocks
    with no page shows no page, rather than claiming page 1.
    """

    page: int | None
    text: str


@dataclass(frozen=True)
class Chunk:
    """One indexable passage, and the page range it spans.

    ``page_first`` and ``page_last`` are equal for a chunk that sits inside one
    page, differ where short pages packed together, and are both None for a
    format without pages.
    """

    ordinal: int
    page_first: int | None
    page_last: int | None
    text: str


@dataclass(frozen=True)
class Extraction:
    """What reading one file produced.

    ``confidence`` is None where a parser was exact -- a PDF text layer either
    decodes or does not -- and carries OCR's own mean score where the text was
    guessed from pixels. That distinction is what lets a result be shown as read
    from a photo rather than quoted as though it were typed.
    """

    extractor: str
    blocks: tuple[Block, ...]
    pages: int | None = None
    confidence: float | None = None

    @property
    def chars(self) -> int:
        """Total characters read, which is what the panel counts."""
        return sum(len(b.text) for b in self.blocks)
