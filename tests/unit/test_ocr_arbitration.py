"""Deciding which pages are worth reading as pictures.

The expensive half of the pipeline hangs off this one predicate, so it is tested
on its own rather than only through a pass. Both kinds of mistake cost real
time: calling an ordinary page a scan spends a second rendering and reading
something with nothing on it, and refusing a real scan leaves the file
unsearchable with nothing saying why.

The false positive is the one to watch. Sparse text is *not* evidence of a scan
-- title pages, section dividers and pages holding a single table all have
almost no extractable characters, and on a long document they are common enough
that treating them as scans would be most of a wasted run.
"""

from __future__ import annotations

import docfixtures as fx
import pytest

from trove.text import extract, pdf
from trove.text.pdf import PageStat, looks_scanned
from trove.text.results import DOCUMENTS, NO_TEXT_LAYER, OCR

MIN_CHARS, MIN_COVER = 40, 0.5


def _verdict(chars: int, cover: float) -> bool:
    return looks_scanned(PageStat(1, chars, cover), MIN_CHARS, MIN_COVER)


# --- the predicate ----------------------------------------------------------


def test_a_scanned_page_is_recognised():
    """No text layer, one image over the whole sheet."""
    assert _verdict(chars=0, cover=1.0) is True
    assert _verdict(chars=12, cover=0.94) is True


def test_a_page_of_real_text_is_left_alone():
    assert _verdict(chars=1800, cover=0.0) is False
    # Text *and* a photograph on the same page: still a document page.
    assert _verdict(chars=1800, cover=0.6) is False


def test_a_title_page_is_not_a_scan():
    """The false positive that would double a run. Almost no characters, and no
    image at all -- there is nothing here for OCR to find."""
    assert _verdict(chars=18, cover=0.0) is False


def test_a_page_with_a_small_figure_and_little_text_is_not_a_scan():
    """A divider carrying a logo. Sparse text plus a *small* image is not a
    picture of a page."""
    assert _verdict(chars=10, cover=0.12) is False


def test_both_conditions_are_required():
    for chars, cover in ((0, 0.0), (0, 0.49), (41, 1.0), (1000, 1.0)):
        assert _verdict(chars, cover) is False, (chars, cover)


# --- against a real scanned PDF --------------------------------------------


def test_a_real_scan_reports_no_text_and_a_full_page_image(tmp_path):
    """The fixture is a PDF whose pages are pictures, written by Pillow -- the
    shape a scanner actually produces."""
    path = fx.scan_pdf(tmp_path / "scan.pdf", ["FACTURA 4471", "Segunda pagina"])
    stats = pdf.page_stats(path)
    assert len(stats) == 2
    for stat in stats:
        assert stat.chars == 0
        assert stat.image_cover > 0.9
        assert looks_scanned(stat, MIN_CHARS, MIN_COVER)


def test_a_born_digital_pdf_reports_text_and_no_image(tmp_path):
    path = fx.pdf(tmp_path / "born.pdf", ["Contrato de arrendamiento firmado en marzo"])
    stat = pdf.page_stats(path)[0]
    assert stat.chars > MIN_CHARS
    assert stat.image_cover == 0.0
    assert not looks_scanned(stat, MIN_CHARS, MIN_COVER)


# --- what each half is offered ---------------------------------------------


def test_pictures_are_only_work_when_text_in_images_is_on():
    assert extract.eligible("jpg", "image", frozenset({DOCUMENTS})) is False
    assert extract.eligible("jpg", "image", frozenset({OCR})) is True
    assert extract.eligible("png", "image", frozenset({DOCUMENTS, OCR})) is True


def test_raw_camera_formats_are_never_read():
    """Photographs of the world by definition, the most expensive to decode, and
    the least likely thing anyone photographed a document with."""
    for ext in ("cr2", "nef", "arw", "dng", "raw"):
        assert extract.eligible(ext, "image", frozenset({OCR})) is False


def test_pdfs_are_work_for_either_half():
    """A PDF may need one, the other, or both -- which is why they share a stage."""
    for wanted in (frozenset({DOCUMENTS}), frozenset({OCR}), frozenset({DOCUMENTS, OCR})):
        assert extract.eligible("pdf", "document", wanted) is True


def test_a_word_file_is_not_work_for_text_in_images_alone():
    """It carries its own text; there is nothing for a reader of pixels to do."""
    assert extract.eligible("docx", "document", frozenset({OCR})) is False
    assert extract.eligible("docx", "document", frozenset({DOCUMENTS})) is True


def test_nothing_is_work_when_neither_half_is_on():
    for ext, media in (("pdf", "document"), ("jpg", "image"), ("docx", "document")):
        assert extract.eligible(ext, media, frozenset()) is False


# --- the reason a file carries when only one half is on --------------------


def test_a_scan_read_without_text_in_images_says_what_it_is(tmp_path):
    """Not a failure, and specifically not permanent: switching the other half
    on has to bring this file back."""
    path = fx.scan_pdf(tmp_path / "scan.pdf", ["FACTURA 4471"])
    with pytest.raises(ValueError, match=NO_TEXT_LAYER) as caught:
        extract.read(path, "pdf", "document", frozenset({DOCUMENTS}))
    assert "pictures of text" in str(caught.value)
