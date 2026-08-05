"""Readers for the two zipped-XML office families, on the standard library.

Word, Excel and PowerPoint files and their OpenDocument counterparts are all a
ZIP of XML. Getting *text* out of them needs the archive opened and the right
parts walked, and nothing else -- so this is `zipfile` and `ElementTree`, and the
`documents` feature adds no dependency for any of the six formats.

That is a smaller claim than it sounds. python-docx, openpyxl and python-pptx
exist to model documents: styles, merged cells, formulas, revision history. None
of that is asked for here. What is asked for is the words, in reading order,
attributed to a page where the format has pages -- which is a walk over the text
nodes, and is why the same 200 lines cover both families rather than one.

Namespaces are matched on local name throughout. The OOXML and ODF namespace
URIs carry version and vendor detail that varies between producers, and matching
the full URI is how a reader ends up silently returning nothing for a file
written by a slightly different version of the same program.

**Legacy .doc / .xls / .ppt are not read here and cannot be.** They are OLE2
compound binaries, not zipped XML, and there is no pure-Python reader worth
shipping for them. They are reported as an unsupported format, which is a
better answer than the partial text a naive strings-style scrape would produce.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from .results import UNSUPPORTED, Block

OOXML_EXTS = frozenset({"docx", "xlsx", "pptx"})
ODF_EXTS = frozenset({"odt", "ods", "odp"})
LEGACY_EXTS = frozenset({"doc", "xls", "ppt"})
OFFICE_EXTS = OOXML_EXTS | ODF_EXTS

# Slides sort numerically, not lexically: slide10 comes after slide9.
_SLIDE = re.compile(r"slide(\d+)\.xml$")


def _local(tag: str) -> str:
    """An element's name without its namespace."""
    return tag.rpartition("}")[2]


def _text_of(node: ElementTree.Element, wanted: str) -> str:
    """All text under ``node`` held in elements named ``wanted``, concatenated."""
    return "".join(e.text or "" for e in node.iter() if _local(e.tag) == wanted)


def _paragraphs(xml: bytes, para: str, text: str) -> list[str]:
    """Every non-empty paragraph in one XML part, in document order."""
    root = ElementTree.fromstring(xml)
    out = []
    for node in root.iter():
        if _local(node.tag) == para:
            line = _text_of(node, text).strip()
            if line:
                out.append(line)
    return out


def _read_docx(zf: zipfile.ZipFile) -> list[Block]:
    """A Word document's body, then whatever its headers and footers add.

    A .docx has no page breaks that survive without laying the document out --
    pagination is the renderer's, not the file's -- so these carry no page.
    """
    names = [n for n in zf.namelist() if n == "word/document.xml"]
    names += sorted(n for n in zf.namelist() if re.fullmatch(r"word/(header|footer)\d*\.xml", n))
    lines = [line for name in names for line in _paragraphs(zf.read(name), "p", "t")]
    return [Block(None, "\n".join(lines))] if lines else []


def _read_pptx(zf: zipfile.ZipFile) -> list[Block]:
    """One block per slide, so a hit can say which slide it was on."""
    slides = sorted(
        (n for n in zf.namelist() if _SLIDE.search(n) and n.startswith("ppt/slides/")),
        key=lambda n: int(_SLIDE.search(n).group(1)),  # type: ignore[union-attr]
    )
    blocks = []
    for number, name in enumerate(slides, start=1):
        lines = _paragraphs(zf.read(name), "p", "t")
        if lines:
            blocks.append(Block(number, "\n".join(lines)))
    return blocks


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    """A workbook's string table, which is where most of its text actually lives."""
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ElementTree.fromstring(zf.read("xl/sharedStrings.xml"))
    return [_text_of(si, "t") for si in root if _local(si.tag) == "si"]


def _read_xlsx(zf: zipfile.ZipFile) -> list[Block]:
    """Every cell's value, sheet by sheet, rows joined by newline.

    Cells are resolved against the string table rather than dumped raw: a cell
    holding a shared string stores an *index* into that table, so reading `<v>`
    verbatim would index a spreadsheet full of small integers. Numbers are kept
    as they are -- an invoice total is exactly the kind of thing someone
    searches a paperwork archive for.
    """
    strings = _shared_strings(zf)
    sheets = sorted(
        n for n in zf.namelist() if n.startswith("xl/worksheets/") and n.endswith(".xml")
    )
    blocks = []
    for name in sheets:
        root = ElementTree.fromstring(zf.read(name))
        rows = []
        for row in (n for n in root.iter() if _local(n.tag) == "row"):
            values = []
            for cell in (c for c in row if _local(c.tag) == "c"):
                if cell.get("t") == "s":
                    index = _text_of(cell, "v")
                    value = (
                        strings[int(index)] if index.isdigit() and int(index) < len(strings) else ""
                    )
                else:
                    value = _text_of(cell, "t") or _text_of(cell, "v")
                if value.strip():
                    values.append(value.strip())
            if values:
                rows.append("\t".join(values))
        if rows:
            blocks.append(Block(None, "\n".join(rows)))
    return blocks


def _read_odf(zf: zipfile.ZipFile) -> list[Block]:
    """Any OpenDocument body: text, spreadsheet or presentation.

    All three keep their content in one `content.xml` and mark every run of text
    with the same `text:p` / `text:span` vocabulary, so one reader covers the
    family. Draw pages are not split out per slide the way .pptx is -- ODP is
    rare enough here that the extra walk is not yet worth its lines.
    """
    if "content.xml" not in zf.namelist():
        return []
    lines = _paragraphs(zf.read("content.xml"), "p", "span")
    body = ElementTree.fromstring(zf.read("content.xml"))
    # A paragraph with no styled span still has its own text; take it when the
    # span walk found nothing, rather than returning an empty document.
    if not lines:
        lines = [
            (node.text or "").strip()
            for node in body.iter()
            if _local(node.tag) == "p" and (node.text or "").strip()
        ]
    return [Block(None, "\n".join(lines))] if lines else []


_READERS = {"docx": _read_docx, "pptx": _read_pptx, "xlsx": _read_xlsx}


def read(path: Path, ext: str) -> list[Block]:
    """One office file as blocks, or raise for a format that cannot be read."""
    if ext in LEGACY_EXTS:
        raise ValueError(
            f"{UNSUPPORTED} legacy Office format (.{ext}): an OLE2 binary, not zipped XML"
        )
    try:
        with zipfile.ZipFile(path) as zf:
            reader = _READERS.get(ext)
            return reader(zf) if reader else _read_odf(zf)
    except zipfile.BadZipFile as exc:
        raise ValueError(f"{UNSUPPORTED} .{ext}: not a readable archive ({exc})") from exc
    except ElementTree.ParseError as exc:
        raise ValueError(f"{UNSUPPORTED} .{ext}: malformed XML inside ({exc})") from exc
