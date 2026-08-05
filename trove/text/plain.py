"""Readers for the formats that are already text, or nearly.

Plain text, Markdown, CSV, HTML and notebooks, all on the standard library. None
of them has pages, so every block they produce carries ``page=None`` -- a result
from a .txt says no page rather than claiming page 1.
"""

from __future__ import annotations

import codecs
import json
from html.parser import HTMLParser
from pathlib import Path

from .results import UNSUPPORTED, Block

# Formats read as text with no structure to unwrap.
FLAT_EXTS = frozenset({"txt", "md", "csv"})
MARKUP_EXTS = frozenset({"html", "htm", "mhtml", "xhtml"})
NOTEBOOK_EXTS = frozenset({"ipynb"})
PLAIN_EXTS = FLAT_EXTS | MARKUP_EXTS | NOTEBOOK_EXTS

# Never rendered, so never text a search should match.
_SKIP_TAGS = frozenset({"script", "style", "head", "noscript", "template"})

_BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


def decode(raw: bytes) -> str:
    """Bytes to text, trusting a BOM first and guessing only after.

    A byte-order mark is a statement rather than a guess, so it wins outright --
    and it has to be checked before UTF-8 is attempted, because a UTF-16 file
    often decodes as UTF-8 without raising and yields text interleaved with NULs.
    UTF-32 is checked before UTF-16 for the same reason in miniature: the
    little-endian UTF-32 mark opens with the little-endian UTF-16 one.

    Falling through to cp1252 with replacement covers what is left -- old
    Windows exports, mostly -- and replacement rather than an error because a
    document with three unreadable bytes is still worth searching.
    """
    for bom, encoding in _BOMS:
        if raw.startswith(bom):
            return raw.decode(encoding, errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


class _TextOnly(HTMLParser):
    """Collects the text a browser would show, and nothing else."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._muted = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in _SKIP_TAGS:
            self._muted += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._muted:
            self._muted -= 1

    def handle_data(self, data: str) -> None:
        if not self._muted and data.strip():
            self.parts.append(data.strip())


def read_markup(raw: bytes) -> list[Block]:
    """The visible text of an HTML page or a saved MHTML archive.

    MHTML is a MIME wrapper around the same HTML, and its base64 parts have no
    tags for the parser to open, so feeding the whole file through gets the
    markup part and ignores the rest -- worth more than a MIME walk for a format
    this rarely turns up.
    """
    parser = _TextOnly()
    parser.feed(decode(raw))
    parser.close()
    return [Block(None, "\n".join(parser.parts))] if parser.parts else []


def read_notebook(raw: bytes) -> list[Block]:
    """A notebook's own text: markdown and code, never the outputs.

    Outputs are the largest part of most notebooks and the least like something
    anyone searches for -- base64 images, tracebacks, thousand-row frames.
    """
    try:
        doc = json.loads(decode(raw))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{UNSUPPORTED} notebook: not valid JSON ({exc.msg})") from exc
    parts: list[str] = []
    for cell in doc.get("cells", []) if isinstance(doc, dict) else []:
        source = cell.get("source", "")
        text = "".join(source) if isinstance(source, list) else str(source)
        if text.strip():
            parts.append(text.strip())
    return [Block(None, "\n\n".join(parts))] if parts else []


def read(path: Path, ext: str) -> list[Block]:
    """One plain-ish file as blocks, dispatched on its extension."""
    raw = path.read_bytes()
    if ext in MARKUP_EXTS:
        return read_markup(raw)
    if ext in NOTEBOOK_EXTS:
        return read_notebook(raw)
    text = decode(raw)
    return [Block(None, text)] if text.strip() else []
