"""Document files built in-test, so no binary fixtures live in the repository.

Every format the Documents feature reads is either a ZIP of XML or plain bytes,
and a PDF with a text layer is short enough to write by hand. Building them here
means a fixture is readable in the diff that changes it, and that adding a case
costs a function rather than a checked-in blob nobody can inspect.

Shared by the unit and integration tiers (``pythonpath = ["tests"]``).
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

# Namespace declarations the readers must tolerate but never match on: the
# fixtures carry real ones so that a reader matching full namespace URIs instead
# of local names fails here rather than on a user's file.
_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_A = (
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
)
_S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
_O = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"'
)


def docx(path: Path, paragraphs: list[str], header: str = "") -> Path:
    """A Word document, optionally with a header part."""
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", f"<w:document {_W}><w:body>{body}</w:body></w:document>")
        if header:
            zf.writestr(
                "word/header1.xml", f"<w:hdr {_W}><w:p><w:r><w:t>{header}</w:t></w:r></w:p></w:hdr>"
            )
    return path


def pptx(path: Path, slides: list[str]) -> Path:
    """A presentation, one string per slide, named so slide10 sorts after slide9."""
    with zipfile.ZipFile(path, "w") as zf:
        for number, text in enumerate(slides, start=1):
            zf.writestr(
                f"ppt/slides/slide{number}.xml",
                f"<p:sld {_A}><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:sld>",
            )
    return path


def xlsx(path: Path, rows: list[list[str]]) -> Path:
    """A workbook whose text cells go through the shared-string table.

    Which is the point: a cell of type "s" holds an *index* into that table, so a
    reader that dumps `<v>` verbatim produces a sheet of small integers and looks
    like it worked.
    """
    strings: list[str] = []
    body = ""
    for r, row in enumerate(rows, start=1):
        cells = ""
        for c, value in enumerate(row):
            ref = f"{chr(ord('A') + c)}{r}"
            if value.replace(".", "").replace(",", "").isdigit():
                cells += f'<c r="{ref}"><v>{value}</v></c>'
            else:
                if value not in strings:
                    strings.append(value)
                cells += f'<c r="{ref}" t="s"><v>{strings.index(value)}</v></c>'
        body += f'<row r="{r}">{cells}</row>'
    table = "".join(f"<si><t>{s}</t></si>" for s in strings)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("xl/sharedStrings.xml", f"<sst {_S}>{table}</sst>")
        zf.writestr(
            "xl/worksheets/sheet1.xml", f"<worksheet {_S}><sheetData>{body}</sheetData></worksheet>"
        )
    return path


def odt(path: Path, paragraphs: list[str]) -> Path:
    """An OpenDocument text file: one content.xml, text in text:p/text:span."""
    body = "".join(f"<text:p><text:span>{p}</text:span></text:p>" for p in paragraphs)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "content.xml", f"<office:document-content {_O}>{body}</office:document-content>"
        )
    return path


def notebook(path: Path, cells: list[str], output: str = "never-searchable") -> Path:
    """A notebook whose cells carry text and whose outputs must be ignored."""
    path.write_text(
        json.dumps(
            {
                "cells": [
                    {
                        "cell_type": "code",
                        "source": [source],
                        "outputs": [{"text": [output]}],
                    }
                    for source in cells
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def pdf(path: Path, pages: list[str]) -> Path:
    """A PDF with a real text layer, one page per string.

    Written by hand rather than by a writer dependency: the whole point of
    choosing pypdfium2 was that reading needs no other library, and a fixture
    builder that pulled one in would put that back.
    """
    objs: list[int] = []
    out = bytearray(b"%PDF-1.4\n")

    def add(body: bytes) -> None:
        objs.append(len(out))
        out.extend(f"{len(objs)} 0 obj\n".encode() + body + b"\nendobj\n")

    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(pages)))
    add(b"<< /Type /Catalog /Pages 2 0 R >>")
    add(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for text in pages:
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        add(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
            f"<< /Font << /F1 3 0 R >> >> /Contents {len(objs) + 2} 0 R >>".encode()
        )
        add(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    start = len(out)
    out.extend(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in objs:
        out.extend(f"{off:010d} 00000 n \n".encode())
    out.extend(
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()
    )
    path.write_bytes(bytes(out))
    return path


def scanned_pdf(path: Path, pages: int = 1) -> Path:
    """A PDF carrying no text layer at all -- what a scan actually looks like.

    Pages with a content stream that draws nothing. Pictures of text is the reader
    for these; Documents has to report that honestly rather than as a failure.
    """
    return pdf(path, [""] * pages)


def scan_pdf(path: Path, pages: list[str], dpi: int = 150) -> Path:
    """A PDF whose pages are *pictures* of text -- what a scanner produces.

    Rendered with Pillow, which embeds each image full-page and writes no text
    layer at all. That is the shape the arbitration has to recognise: almost no
    extractable characters, and one image covering the sheet.
    """
    from PIL import Image, ImageDraw, ImageFont

    size = (int(8.27 * dpi), int(11.69 * dpi))  # A4
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", dpi // 5)
    except OSError:  # pragma: no cover - depends on the machine's fonts
        font = ImageFont.load_default()

    rendered = []
    for body in pages:
        img = Image.new("RGB", size, "white")
        draw = ImageDraw.Draw(img)
        y = dpi
        for line in body.splitlines():
            draw.text((dpi // 2, y), line, font=font, fill="black")
            y += dpi // 3
        rendered.append(img)
    rendered[0].save(path, "PDF", save_all=True, append_images=rendered[1:], resolution=dpi)
    return path


def photo(path: Path, size: tuple[int, int] = (900, 700)) -> Path:
    """A picture with no writing anywhere in it -- the overwhelmingly common case."""
    import random

    from PIL import Image

    random.seed(11)
    img = Image.new("RGB", size)
    px = img.load()
    for x in range(0, size[0], 6):
        for y in range(0, size[1], 6):
            colour = (random.randint(60, 200), random.randint(80, 210), random.randint(90, 220))
            for a in range(6):
                for b in range(6):
                    if x + a < size[0] and y + b < size[1]:
                        px[x + a, y + b] = colour
    img.save(path)
    return path
