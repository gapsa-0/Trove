"""Reading text out of pixels, against the real models.

Marked ``models`` and ``slow`` because it runs three ONNX sessions over real
images. What it pins is the two claims the design rests on: the reader handles
accented Spanish, and reading at two resolutions gives the same text as reading
everything at full size — which is the whole justification for a design that is
otherwise just more complicated.
"""

from __future__ import annotations

import docfixtures as fx
import pytest

from trove.text import ocr, raster
from trove.text.results import IMAGE_OCR

pytest.importorskip("numpy")
pytest.importorskip("PIL")

pytestmark = [
    pytest.mark.models,
    pytest.mark.slow,
    pytest.mark.skipif(not ocr.available(), reason="the 'ocr' extra is not installed"),
]

SPANISH = [
    "FACTURA N.º 2024/00471",
    "Importe del alquiler mensual: 850,00 EUR",
    "Petición de reembolso según el artículo duodécimo.",
    "Señor Muñoz, NIF B-12345678.",
]


@pytest.fixture(scope="module")
def scan(tmp_path_factory):
    return fx.scan_pdf(tmp_path_factory.mktemp("ocr") / "scan.pdf", ["\n".join(SPANISH)], dpi=200)


def _text(path):
    lines, confidence = ocr.read_array(raster.pdf_page(path, 1))
    return " ".join(lines), confidence


# --- what it reads ----------------------------------------------------------


def test_it_reads_accented_spanish(scan):
    """The reason no per-language weight is needed: one bundled model carries
    18,708 characters, every Spanish accent among them."""
    text, _ = _text(scan)
    for word in ("Petición", "según", "artículo", "Señor", "Muñoz"):
        assert word.lower() in text.lower(), f"{word} missing from: {text}"


def test_it_reads_the_numbers_someone_would_search_for(scan):
    text, _ = _text(scan)
    for token in ("850,00", "B-12345678", "2024/00471"):
        assert token.replace(" ", "") in text.replace(" ", ""), f"{token} missing from: {text}"


def test_a_reading_carries_its_confidence(scan):
    """Stored per file, and what lets a result be shown as read from a picture
    rather than quoted as though it were typed."""
    _, confidence = _text(scan)
    assert confidence is not None
    assert 0.0 < confidence <= 1.0
    assert confidence > 0.7, "clean rendered text should read with high confidence"


def test_a_picture_with_no_writing_reads_as_nothing(tmp_path):
    """The overwhelmingly common case, and not a failure. None rather than an
    empty extraction, so the caller records a skip with a reason."""
    assert ocr.read_image(fx.photo(tmp_path / "photo.png")) is None


def test_a_photographed_document_comes_back_as_one_block(tmp_path):
    path = tmp_path / "receipt.png"
    from PIL import Image

    page = raster.pdf_page(fx.scan_pdf(tmp_path / "s.pdf", ["\n".join(SPANISH)], dpi=200), 1)
    Image.fromarray(page).save(path)

    found = ocr.read_image(path)
    assert found is not None
    assert found.extractor == IMAGE_OCR
    assert len(found.blocks) == 1
    # A picture has no pages, so it claims none rather than inventing page 1.
    assert found.blocks[0].page is None
    assert found.confidence is not None


# --- the two-resolution design ---------------------------------------------


def test_detecting_small_and_recognising_large_reads_the_same_text(scan):
    """The claim the design rests on. Detection runs on a shrunken copy because
    its cost is set by input size and it runs on *every* image; recognition runs
    on crops of the original so small print stays legible.

    Measured while building this: on a 2480x3508 scan the two paths return
    byte-identical text, and a picture with no writing costs 0.59s instead of
    1.51s. If that ever stops holding, the extra complexity has stopped paying.
    """
    page = raster.pdf_page(scan, 1)
    downscaled, _ = ocr.read_array(page, detect_side=736)
    full_size, _ = ocr.read_array(page, detect_side=max(page.shape[:2]))
    assert downscaled == full_size


def test_an_image_already_smaller_than_the_detect_side_is_not_resized(tmp_path):
    """No upscaling, and no pointless copy: a screenshot is often already small.

    Rendered at 50 dpi rather than authored small, because the rasteriser always
    renders at its own resolution whatever dpi the PDF was written with.
    """
    page = raster.pdf_page(fx.scan_pdf(tmp_path / "s.pdf", ["Recibo 4471"], dpi=200), 1, dpi=50)
    assert max(page.shape[:2]) < 736

    unchanged, scale = ocr._downscaled(page, 736)
    assert scale == 1.0
    assert unchanged is page, "an image under the limit is passed through, not copied"


def test_low_confidence_lines_are_dropped(tmp_path, monkeypatch):
    """The low tail of the confidence range is where the recogniser invents
    words out of texture -- foliage, fabric, wallpaper. That noise in a photo
    archive's index would make it worthless."""
    monkeypatch.setattr(ocr, "MIN_LINE_CONFIDENCE", 1.01)
    page = raster.pdf_page(fx.scan_pdf(tmp_path / "s.pdf", ["Recibo 4471"], dpi=200), 1)
    lines, confidence = ocr.read_array(page)
    assert lines == []
    assert confidence is None
