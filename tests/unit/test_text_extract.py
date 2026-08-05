"""Reading the text out of each format the Documents feature claims to handle.

One fixture per format, built in-test (``tests/docfixtures.py``). The claim
being checked is narrow and worth stating: the words come out, in reading order,
attributed to a page where the format has pages. Formatting, styles and formulas
are not read and are not wanted.

The failure cases carry as much weight as the successes here. A reader that
returns nothing for a file it does not understand is indistinguishable from one
reading an empty document, and the difference decides whether the file is ever
looked at again.
"""

from __future__ import annotations

import docfixtures as fx
import pytest

from trove.text import extract, office, plain
from trove.text.results import (
    DOCUMENTS,
    NO_TEXT_LAYER,
    OFFICE,
    OPENDOCUMENT,
    PDF_TEXT,
    PLAIN,
    UNSUPPORTED,
)

WANTED = frozenset({DOCUMENTS})


def _read(path, ext, media_type="document", wanted=WANTED, **limits):
    return extract.read(
        path, ext, media_type, wanted, limits=extract.Limits(**limits) if limits else None
    )


def _text(path, ext) -> str:
    return "\n".join(b.text for b in _read(path, ext).blocks)


# --- the formats that work -------------------------------------------------


def test_a_word_document_reads_its_body_and_its_header(tmp_path):
    path = fx.docx(
        tmp_path / "a.docx", ["Contrato de arrendamiento", "Clausula segunda"], "Membrete"
    )
    result = _read(path, "docx")
    assert result.extractor == OFFICE
    assert "Contrato de arrendamiento" in result.blocks[0].text
    assert "Clausula segunda" in result.blocks[0].text
    assert "Membrete" in result.blocks[0].text
    # Word has no pages until something lays it out, so it claims none.
    assert result.blocks[0].page is None


def test_a_presentation_attributes_each_slide_to_its_own_page(tmp_path):
    path = fx.pptx(tmp_path / "a.pptx", [f"Diapositiva {n}" for n in range(1, 12)])
    result = _read(path, "pptx")
    assert [b.page for b in result.blocks] == list(range(1, 12))
    # slide10 must sort after slide9, not between slide1 and slide2.
    assert result.blocks[9].text == "Diapositiva 10"


def test_a_workbook_resolves_its_shared_strings(tmp_path):
    """A text cell stores an index into the string table, so a reader that took
    `<v>` at face value would produce a spreadsheet of small integers -- and look
    like it had worked."""
    path = fx.xlsx(tmp_path / "a.xlsx", [["Concepto", "Importe"], ["Alquiler", "1240.55"]])
    body = _text(path, "xlsx")
    assert "Concepto" in body and "Alquiler" in body
    assert "1240.55" in body, "numbers are what someone searches a paperwork archive for"
    assert "\n" in body, "rows are separated"


def test_an_opendocument_file_reads_its_paragraphs(tmp_path):
    path = fx.odt(tmp_path / "a.odt", ["Primera linea", "Segunda linea"])
    result = _read(path, "odt")
    assert result.extractor == OPENDOCUMENT
    assert "Primera linea" in result.blocks[0].text


def test_a_pdf_reads_one_block_per_page_with_text(tmp_path):
    path = fx.pdf(tmp_path / "a.pdf", ["Pagina uno del contrato", "Pagina dos del contrato"])
    result = _read(path, "pdf")
    assert result.extractor == PDF_TEXT
    assert result.pages == 2
    assert [b.page for b in result.blocks] == [1, 2]
    assert "Pagina uno" in result.blocks[0].text


@pytest.mark.parametrize("ext", ["txt", "md", "csv"])
def test_the_flat_text_formats_read_verbatim(tmp_path, ext):
    path = tmp_path / f"a.{ext}"
    path.write_text("Recibo numero 4471\nTotal 1240,55", encoding="utf-8")
    result = _read(path, ext)
    assert result.extractor == PLAIN
    assert "4471" in result.blocks[0].text


def test_html_keeps_what_is_shown_and_drops_what_is_not(tmp_path):
    path = tmp_path / "a.html"
    path.write_text(
        "<html><head><title>t</title><style>body{color:red}</style></head>"
        "<body><script>var secreto = 1;</script><p>Texto visible</p></body></html>",
        encoding="utf-8",
    )
    body = _text(path, "html")
    assert "Texto visible" in body
    assert "secreto" not in body and "color:red" not in body


def test_a_notebook_reads_its_cells_and_never_its_outputs(tmp_path):
    """Outputs are the bulk of most notebooks and the least like anything anyone
    searches for -- base64 images, tracebacks, thousand-row frames."""
    path = fx.notebook(tmp_path / "a.ipynb", ["import pandas", "df.describe()"])
    body = _text(path, "ipynb")
    assert "import pandas" in body and "df.describe()" in body
    assert "never-searchable" not in body


# --- encodings -------------------------------------------------------------


def test_a_utf16_file_is_not_read_as_utf8(tmp_path):
    """UTF-16 usually decodes as UTF-8 without raising, giving text interleaved
    with NULs -- which indexes as garbage rather than failing."""
    path = tmp_path / "a.txt"
    path.write_bytes("Petición de reembolso".encode("utf-16"))
    body = _text(path, "txt")
    assert body.startswith("Petición de reembolso")
    assert "\x00" not in body


def test_a_windows_encoded_file_still_reads(tmp_path):
    path = tmp_path / "a.txt"
    path.write_bytes("Peticion de reembolso — anexo".encode("cp1252"))
    assert "reembolso" in _text(path, "txt")


def test_a_utf8_bom_is_not_read_as_content(tmp_path):
    path = tmp_path / "a.txt"
    path.write_bytes(b"\xef\xbb\xbfFactura")
    assert _text(path, "txt").startswith("Factura")


def test_decode_prefers_a_bom_over_guessing():
    assert plain.decode("hola".encode("utf-16")) == "hola"
    assert plain.decode("hola".encode("utf-8-sig")) == "hola"
    assert plain.decode(b"hola") == "hola"


# --- the failures, which have to be told apart -----------------------------


@pytest.mark.parametrize("ext", sorted(office.LEGACY_EXTS))
def test_a_legacy_office_file_is_refused_by_name(tmp_path, ext):
    """.doc/.xls/.ppt are OLE2 binaries with no pure-Python reader worth
    shipping. Saying so per file is what makes the panel's claim checkable where
    the file is."""
    path = tmp_path / f"a.{ext}"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    with pytest.raises(ValueError, match=UNSUPPORTED):
        _read(path, ext)


def test_a_pdf_with_no_text_layer_is_a_skip_and_not_a_failure(tmp_path):
    """This is what a scan is, and it is the single most important distinction
    this module draws: nothing is wrong with the file, it just needs the other
    reader. Reporting it as an error would put a red count on the Documents card
    for an archive working exactly as intended."""
    path = fx.scanned_pdf(tmp_path / "scan.pdf", pages=2)
    with pytest.raises(ValueError, match=NO_TEXT_LAYER):
        _read(path, "pdf")


def test_a_corrupt_office_file_says_so_rather_than_reading_empty(tmp_path):
    path = tmp_path / "a.docx"
    path.write_bytes(b"this is not a zip archive at all")
    with pytest.raises(ValueError, match=UNSUPPORTED):
        _read(path, "docx")


def test_a_file_over_the_size_limit_is_skipped_before_it_is_opened(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("x" * 5000, encoding="utf-8")
    with pytest.raises(ValueError, match="media exceeds"):
        _read(path, "txt", max_bytes=1000)


def test_an_empty_document_is_a_skip_rather_than_an_empty_index_entry(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("   \n\n  ", encoding="utf-8")
    with pytest.raises(ValueError, match=NO_TEXT_LAYER):
        _read(path, "txt")


# --- what counts as work at all --------------------------------------------


def test_only_documents_are_eligible_while_only_documents_is_on(tmp_path):
    assert extract.eligible("pdf", "document", WANTED) is True
    assert extract.eligible("docx", "document", WANTED) is True
    # An image is work for Pictures of text, which is not switched on here.
    assert extract.eligible("jpg", "image", WANTED) is False
    # Nothing is work when neither half is on.
    assert extract.eligible("pdf", "document", frozenset()) is False


def test_a_legacy_file_is_eligible_so_that_it_can_be_reported(tmp_path):
    """It cannot be read, but it has to enter the backlog once to get a row
    saying why -- otherwise the panel's claim about legacy formats is invisible
    on the archive that actually holds them."""
    assert extract.eligible("doc", "document", WANTED) is True


def test_an_archive_or_a_video_is_never_text_work():
    for ext, media in (("zip", "archive"), ("mp4", "video"), ("mp3", "audio")):
        assert extract.eligible(ext, media, WANTED) is False
