"""A small Markdown renderer for the documentation pages, and nothing else.

The app has no runtime dependencies and no frontend build step (ADR 0002), so
the reference pages under ``web/docs/`` are turned into HTML here rather than by
a library or a bundler. That is only affordable because the input is not
arbitrary Markdown: every byte of it is written in this repository, so this
needs to cover the constructs those pages actually use, not CommonMark.

What it supports, and therefore what a documentation page may use:

* front matter -- a ``---`` fenced block of ``key: value`` lines, at the top
* ATX headings, ``#`` to ``####``
* paragraphs, ``---`` rules, and ``>`` blockquotes
* unordered (``-``) and ordered (``1.``) lists, one level
* GFM pipe tables, with the ``---`` alignment row
* fenced code blocks; a ``scale`` info string is a calibration figure instead
  (see ``_scale``), which is why the fence is worth having at all
* inline ``**bold**``, ``*italic*``, ``` `code` ``` and ``[text](target)``

Anything else is passed through as literal text, escaped. That is the important
half of the contract: an unsupported construct must look wrong on the page
rather than disappear from it, and ``tests/unit/test_markdown.py`` checks the
shipped pages against it so a page cannot quietly lose a table.

Every value that reaches the output goes through ``_esc`` first and link targets
are filtered by ``_LINK_OK``, so a page cannot introduce markup or a scheme of
its own -- not because the input is untrusted, but because a renderer that
happens to be safe only while nobody makes a mistake is not worth writing.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

# What a link may point at: another documentation page, an anchor on this one,
# or an external https URL. Anything else (javascript:, data:, http:) is dropped
# to plain text, keeping its label.
# One number from a `scale` block: its value, and exactly how the page wrote it.
# Both halves are load-bearing -- see the comment in ``_scale_parts``.
_Num = tuple[float, str]

_LINK_OK = re.compile(r"\A(?:#[\w/-]*|[\w-]+\.md(?:#[\w-]+)?|https://[^\s<>\"]+)\Z")
_HEADING = re.compile(r"\A(#{1,4})\s+(.+?)\s*\Z")
_FENCE = re.compile(r"\A```([\w-]*)\s*\Z")
_ORDERED = re.compile(r"\A(\d+)\.\s+(.*)\Z")
_RULE = re.compile(r"\A-{3,}\s*\Z")
_TABLE_ALIGN = re.compile(r"\A\|?[\s:|-]+\|[\s:|-]*\Z")
# Inline spans, longest marker first so ``**`` never matches as two ``*``.
_INLINE = re.compile(
    r"`([^`]+)`"  # code
    r"|\*\*([^*]+)\*\*"  # bold
    r"|\*([^*]+)\*"  # italic
    r"|\[([^\]]+)\]\(([^)]+)\)"  # link
)
_SLUG_STRIP = re.compile(r"[^a-z0-9\s-]")
# What a calibration band may be drawn as, beyond the default. "" is the
# accent: the population the figure is about. See ``_scale``.
_TONES = ("muted", "soft")


@dataclass
class Page:
    """One rendered documentation page: its front matter, body, and outline."""

    meta: dict[str, str]
    html: str
    # Every ``##`` heading, in order, as ``(anchor, text)``. The reader's
    # on-this-page rail is built from this rather than from the DOM, so the
    # outline exists before the article is inserted and cannot drift from it.
    outline: list[tuple[str, str]] = field(default_factory=list)


def slug(text: str) -> str:
    """A heading's anchor id: lowercase, punctuation dropped, spaces hyphenated."""
    return _SLUG_STRIP.sub("", text.lower()).strip().replace(" ", "-") or "section"


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _inline(text: str) -> str:
    """Escape a run of text and apply the inline spans to it.

    Escaping happens per literal segment rather than once up front, because
    ``_INLINE`` has to match the source characters -- a link's ``]`` and ``)``
    survive escaping, but running the pattern over already-escaped text would
    have ``&amp;`` in the middle of every label for no reason.
    """
    out, at = [], 0
    for m in _INLINE.finditer(text):
        out.append(_esc(text[at : m.start()]))
        code, bold, italic, label, target = m.groups()
        if code is not None:
            out.append(f"<code>{_esc(code)}</code>")
        elif bold is not None:
            out.append(f"<strong>{_inline(bold)}</strong>")
        elif italic is not None:
            out.append(f"<em>{_inline(italic)}</em>")
        elif _LINK_OK.match(target or ""):
            href = _esc(_href(target or ""))
            ext = (
                ' target="_blank" rel="noopener noreferrer"' if target.startswith("https:") else ""
            )
            out.append(f'<a href="{href}"{ext}>{_inline(label or "")}</a>')
        else:
            out.append(_inline(label or ""))  # unusable target: keep the words
        at = m.end()
    out.append(_esc(text[at:]))
    return "".join(out)


def _href(target: str) -> str:
    """Resolve a link target to what the reader should actually navigate to.

    A page links to its neighbours the way the repository reads them --
    ``[Duplicates](duplicates.md)`` renders on a Git host as well as here -- so
    the ``.md`` form is rewritten to this app's hash route on the way out.
    """
    if target.endswith(".md") or ".md#" in target:
        name, _, anchor = target.partition("#")
        return f"#/docs/{name.removesuffix('.md')}" + (f"#{anchor}" if anchor else "")
    return target


def _front_matter(lines: list[str]) -> tuple[dict[str, str], int]:
    """Read the leading ``---`` block as ``key: value`` pairs, if there is one."""
    if not lines or lines[0].strip() != "---":
        return {}, 0
    meta: dict[str, str] = {}
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return meta, i + 1
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip()
    return {}, 0  # unterminated: treat the whole thing as body rather than eat it


def _cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _table(rows: list[str]) -> str:
    head, *body = rows
    ths = "".join(f"<th>{_inline(c)}</th>" for c in _cells(head))
    trs = "".join(
        "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in _cells(r)) + "</tr>" for r in body
    )
    return f'<div class="doc-tablewrap"><table><thead><tr>{ths}</tr></thead><tbody>{trs}</tbody></table></div>'


def _scale_parts(
    body: list[str],
) -> tuple[_Num, _Num, list[tuple[_Num, _Num, str, str]], list[tuple[_Num, str]], str]:
    """The four line kinds a ``scale`` block may hold, read into their parts.

        range 0 1
        band 0 0.30 muted Different animals
        band 0.80 0.96 The same animal
        mark 0.75 pets_cluster_similarity
        note Measured on one archive's own photos.

    A band's optional tone is the first word after its two numbers, from
    ``_TONES``; anything else there is read as the start of the label. The tone
    carries meaning rather than decoration -- ``muted`` is the population the
    threshold is there to exclude, ``soft`` a middle tier that is neither.

    ``note`` says where the numbers came from. A band drawn without one is a
    measurement claim with no source, which is exactly the kind of thing this
    page is supposed to stop being.

    Unknown line kinds and malformed ones are skipped rather than raised on: a
    documentation page is rendered on request, and a typo in a figure should
    cost that figure, not the page someone was trying to read.
    """
    # Every number is carried as (value, as-written). The page and the settings
    # table beside it have to agree character for character -- reformatting
    # `0.80` to `0.8` for the figure while the table says `0.80` reads as two
    # different numbers to anyone checking one against the other.
    lo, hi, bands, marks, note = (0.0, "0"), (1.0, "1"), [], [], ""
    for line in body:
        kind, _, rest = line.strip().partition(" ")
        parts = rest.split(None, 2)
        if kind == "range" and len(parts) >= 2:
            lo, hi = (float(parts[0]), parts[0]), (float(parts[1]), parts[1])
        elif kind == "band" and len(parts) >= 3:
            tone, _, label = parts[2].partition(" ")
            if tone not in _TONES:
                tone, label = "", parts[2]
            bands.append(((float(parts[0]), parts[0]), (float(parts[1]), parts[1]), tone, label))
        elif kind == "mark" and len(parts) >= 1:
            marks.append(((float(parts[0]), parts[0]), parts[1] if len(parts) > 1 else ""))
        elif kind == "note":
            note = rest
    return lo, hi, bands, marks, note


def _scale(body: list[str]) -> str:
    """A calibration figure: where a threshold sits between the values it separates.

    This is the one construct here that is not a Markdown feature, and it exists
    because the thresholds in this app are not arbitrary -- ``config/settings.py``
    records what was measured on either side of nearly every one of them ("same
    animal ~0.8-0.96, different <=~0.3"). Printing only the number throws that
    away; drawing it shows the reader how much room the cut actually has, which
    is the thing they want to know when a photo lands in the wrong group.

    It is written as a fenced block with a ``scale`` info string so the page
    stays valid Markdown: on a Git host the figure degrades to a legible code
    block rather than to nothing. ``_scale_parts`` documents the four lines it
    may hold.

    Band labels are listed under the track rather than written along it: three
    bands on one line collide at any width, and a key also survives the narrow
    layout, where the track is a few hundred pixels wide.

    Marks are drawn ON the track, so two of them closer together than about a
    tenth of the range will overlap. That is an authoring constraint, not
    something to solve here -- a figure needing two cuts that close is a figure
    making two points, and should be two figures.
    """
    lo, hi, bands, marks, note = _scale_parts(body)
    span = (hi[0] - lo[0]) or 1.0
    pct = lambda v: 100 * (v[0] - lo[0]) / span  # noqa: E731 -- one expression, used five times
    track = "".join(
        f'<span class="doc-scale-band {tone}" '
        f'style="left:{pct(a):.4g}%;width:{pct(b) - pct(a):.4g}%"></span>'
        for a, b, tone, _ in bands
    ) + "".join(
        f'<span class="doc-scale-mark" style="left:{pct(v):.4g}%">'
        f'<span class="doc-scale-mark-value">{_esc(v[1])}</span>'
        f'<span class="doc-scale-mark-label">{_esc(label)}</span></span>'
        for v, label in marks
    )
    key = "".join(
        f'<li><i class="{tone}"></i><span>{_inline(label)}</span>'
        f"<b>{_esc(a[1])}&ndash;{_esc(b[1])}</b></li>"
        for a, b, tone, label in bands
    )
    ends = (
        f'<span class="doc-scale-end">{_esc(lo[1])}</span>'
        f'<span class="doc-scale-end">{_esc(hi[1])}</span>'
    )
    caption = f"<figcaption>{_inline(note)}</figcaption>" if note else ""
    return (
        f'<figure class="doc-scale"><span class="doc-scale-track">{track}</span>'
        f'<span class="doc-scale-ends">{ends}</span>'
        f"{f'<ul class="doc-scale-key">{key}</ul>' if key else ''}{caption}</figure>"
    )


@dataclass
class _Blocks:
    """The half-finished block the renderer is currently filling, and the output.

    Markdown's block elements are open-ended: a paragraph, list, quote or table
    ends because something *else* starts, not because it says so. So the loop
    has to carry whichever one is open, and every branch that begins a new block
    has to close the last one first.

    That state lived as six locals and a closure inside ``render``, which is
    what made the function long enough to hide its own shape. Named here, the
    loop reads as what it is -- a dispatch on the line -- and this class holds
    the only thing it accumulates.
    """

    out: list[str] = field(default_factory=list)
    outline: list[tuple[str, str]] = field(default_factory=list)
    para: list[str] = field(default_factory=list)
    table: list[str] = field(default_factory=list)
    items: list[str] = field(default_factory=list)
    list_tag: str = ""
    quote: list[str] = field(default_factory=list)

    def flush(self) -> None:
        """Close whichever block is open. Called before every block-level change."""
        if self.para:
            self.out.append(f"<p>{_inline(' '.join(self.para))}</p>")
            self.para.clear()
        if self.table:
            self.out.append(_table(self.table))
            self.table.clear()
        if self.items:
            self.out.append(
                f"<{self.list_tag}>"
                + "".join(f"<li>{_inline(i)}</li>" for i in self.items)
                + f"</{self.list_tag}>"
            )
            self.items.clear()
            self.list_tag = ""
        if self.quote:
            self.out.append(f"<blockquote><p>{_inline(' '.join(self.quote))}</p></blockquote>")
            self.quote.clear()

    def heading(self, level: int, text: str) -> None:
        """One heading, and its entry in the page outline if it is an ``h2``.

        Only ``h2`` earns an outline row: the sidebar is a page's sections, and
        listing every ``h3`` under them turns a nine-line contents into forty.
        """
        anchor = slug(text)
        if level == 2:
            self.outline.append((anchor, text))
        self.out.append(f'<h{level} id="{_esc(anchor)}">{_inline(text)}</h{level}>')


def _fenced(lines: list[str], at: int, lang: str) -> tuple[str, int]:
    """One fenced block's HTML, and the line after its closing fence.

    ``at`` is the line *after* the opening fence. An unterminated fence runs to
    the end of the page rather than raising: the alternative is a page that
    refuses to render because someone forgot three backticks.
    """
    body: list[str] = []
    while at < len(lines) and not _FENCE.match(lines[at].strip()):
        body.append(lines[at])
        at += 1
    if lang == "scale":
        return _scale(body), at + 1
    cls = f' class="lang-{_esc(lang)}"' if lang else ""
    return f"<pre><code{cls}>{_esc(chr(10).join(body))}</code></pre>", at + 1


def render(text: str) -> Page:
    """Turn one documentation page's Markdown into front matter plus HTML."""
    lines = text.replace("\r\n", "\n").split("\n")
    meta, at = _front_matter(lines)
    b = _Blocks()

    while at < len(lines):
        stripped = lines[at].strip()

        fence = _FENCE.match(stripped)
        if fence:
            b.flush()
            html_, at = _fenced(lines, at + 1, fence.group(1))
            b.out.append(html_)
            continue

        if not stripped:
            b.flush()
            at += 1
            continue

        heading = _HEADING.match(stripped)
        if heading:
            b.flush()
            b.heading(len(heading.group(1)), heading.group(2))
            at += 1
            continue

        # A rule, but only where it cannot be a table's alignment row -- which is
        # the same three hyphens, and is the row that tells a paragraph of pipes
        # from a table.
        if _RULE.match(stripped) and not b.table:
            b.flush()
            b.out.append("<hr>")
            at += 1
            continue

        if stripped.startswith("|"):
            if _TABLE_ALIGN.match(stripped) and b.table:
                at += 1  # alignment row: consumed, not rendered
                continue
            if not b.table:
                b.flush()
            b.table.append(stripped)
            at += 1
            continue

        if stripped.startswith("> "):
            if not b.quote:
                b.flush()
            b.quote.append(stripped[2:])
            at += 1
            continue

        ordered = _ORDERED.match(stripped)
        if stripped.startswith("- ") or ordered:
            tag = "ol" if ordered else "ul"
            if b.list_tag != tag:
                b.flush()
                b.list_tag = tag
            b.items.append(ordered.group(2) if ordered else stripped[2:])
            at += 1
            continue

        # A continuation line inside a list item or a quote belongs to it, not to
        # a new paragraph: a page wraps its prose at the repo's column width, and
        # a wrapped bullet must not break the list in half.
        if b.items:
            b.items[-1] += " " + stripped
        elif b.quote:
            b.quote.append(stripped)
        else:
            b.para.append(stripped)
        at += 1

    b.flush()
    return Page(meta=meta, html="".join(b.out), outline=b.outline)
