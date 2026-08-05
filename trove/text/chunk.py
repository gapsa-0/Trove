"""Cutting a reading into the passages that get indexed and embedded.

Two things force chunking, and they want the same thing. The text embedder has a
512-token window, so a 40-page contract cannot be one vector. And a search result
is only useful as a passage: "page 12, this paragraph" is an answer, where "this
PDF contains your words somewhere" is a place to start looking. So a chunk is
sized for the embedder and carries the page range it came from, and both the
snippet and the page number in a result fall out of the same row.

The sizes are characters rather than tokens on purpose. Tokenising to decide
where to cut would mean loading a 17 MB tokenizer to read a .txt file, tying the
Documents feature to a model it does not otherwise need.

**The rate is not constant, and no character count can bound it.** Measured
against the multilingual-e5-small tokenizer that Search by meaning uses, on
chunks this module actually produced:

    Spanish prose       1193 chars -> 262 tokens    4.6 chars/token
    Spanish, accented   1143 ->  260                4.4
    English prose       1127 ->  284                4.0
    Invoice lines       1196 ->  479                2.5
    CSV rows            1195 ->  628                1.9

Digits, punctuation and delimiters tokenise near-individually, so the densest
text in a paperwork archive costs well over twice per character what prose does
-- and paperwork is what this feature exists for. A .csv is a `document` here,
and a spreadsheet exported to one is the worst case above.

So 1200 is not a size that guarantees the window; it is the size that keeps
prose chunks worth reading (a snippet is shown to a human) while putting the
common dense case just inside it. There is no number that would guarantee it:
the ratio has no floor, and chasing the worst case would make a prose chunk 250
tokens of a 512-token window, doubling the vector count to protect against a
CSV.

**Bounding tokens is therefore the embedding stage's job, not this one's.** It
must measure each chunk against the real tokenizer and sub-split what does not
fit, rather than handing it over to be truncated in silence -- which is what a
tokenizer does with a long input, with no error anywhere and the tail of the
passage simply unsearchable.
"""

from __future__ import annotations

import bisect
import re

from .results import Block, Chunk

# Paragraph, then sentence, then any whitespace: the boundaries a chunk is
# allowed to end on, best first. Sentence-end deliberately requires the
# following space, so a decimal, an ellipsis or "S.L." is not a boundary.
#
# The closing quotes are spelled as escapes because ruff reads a literal
# curly quote in source as a probable typo for a backtick. They are neither --
# a quoted sentence really does end after the quote mark, and dropping them
# would move that boundary to the next space instead.
_CLOSERS = "'\"\u2019\u201d)]"
_PARAGRAPH = re.compile(r"\n\s*\n")
_SENTENCE = re.compile(rf"[.!?;:][{re.escape(_CLOSERS)}]?\s")
_WHITESPACE = re.compile(r"\s")

# Below this fraction of the target a boundary is not worth taking: a chunk that
# ends early because one short paragraph happened to sit there costs a whole
# extra vector for the tail it left behind.
_MIN_FRACTION = 0.75


def _flatten(blocks: list[Block]) -> tuple[str, list[int], list[int | None]]:
    """Join blocks into one stream, remembering which page each one started at.

    Returns the text, the start offset of each block, and each block's page. The
    offsets are what ``_page_span`` binary-searches, so a chunk can name its page
    range without carrying a page per character.
    """
    parts: list[str] = []
    starts: list[int] = []
    pages: list[int | None] = []
    at = 0
    for block in blocks:
        if not block.text.strip():
            continue
        if parts:
            parts.append("\n\n")
            at += 2
        starts.append(at)
        pages.append(block.page)
        parts.append(block.text)
        at += len(block.text)
    return "".join(parts), starts, pages


def _page_span(
    starts: list[int], pages: list[int | None], begin: int, end: int
) -> tuple[int | None, int | None]:
    """The first and last page a chunk touches, or ``(None, None)``.

    None on both sides for a format with no pages at all -- a .txt, a
    spreadsheet -- rather than a fabricated page 1. A chunk that spans a page
    break reports both ends, which is what lets a snippet say "pp. 4-5".
    """
    first = max(0, bisect.bisect_right(starts, begin) - 1)
    last = max(0, bisect.bisect_left(starts, end) - 1)
    touched = [p for p in pages[first : last + 1] if p is not None]
    if not touched:
        return None, None
    return min(touched), max(touched)


def _cut(text: str, begin: int, target: int, hard_cap: int) -> int:
    """Where the chunk starting at ``begin`` should end.

    Aims at ``target`` rather than at the cap: the best boundary at or before it,
    and only if there is none does it accept the first boundary after. Reaching
    for the *latest* boundary under the cap instead would look equivalent and is
    not -- it lands every chunk within a few characters of the ceiling, which is
    exactly where the embedder's token window is at risk. The cap is a limit, not
    a goal.

    Falling through every pattern means the text offered no whitespace at all in
    a cap's worth of characters -- a base64 blob, an unspaced script. Cutting
    mid-token there beats keeping a chunk that overruns the window and loses
    everything past it.
    """
    if begin + hard_cap >= len(text):
        return len(text)
    floor = begin + int(target * _MIN_FRACTION)
    ideal = begin + target
    window = text[begin : begin + hard_cap]
    for pattern in (_PARAGRAPH, _SENTENCE, _WHITESPACE):
        ends = [begin + m.end() for m in pattern.finditer(window)]
        under = [at for at in ends if floor < at <= ideal]
        if under:
            return max(under)
        over = [at for at in ends if at > ideal]
        if over:
            return min(over)
    return begin + hard_cap


def chunk_blocks(
    blocks: list[Block], *, target: int = 1200, overlap: int = 200, hard_cap: int = 1500
) -> list[Chunk]:
    """Cut a file's blocks into overlapping, page-attributed passages.

    ``overlap`` is what stops a sentence that straddles a boundary from being
    findable from neither side: each chunk after the first reaches back into its
    predecessor, snapped forward to a word boundary so it never opens mid-word.

    A file whose text is empty or only whitespace produces no chunks at all,
    rather than one empty one -- there is nothing to match and nothing to show.
    """
    text, starts, pages = _flatten(blocks)
    if not text.strip():
        return []

    chunks: list[Chunk] = []
    begin = 0
    while begin < len(text):
        end = _cut(text, begin, target, hard_cap)
        body = text[begin:end].strip()
        if body:
            first, last = _page_span(starts, pages, begin, end)
            chunks.append(Chunk(len(chunks), first, last, body))
        if end >= len(text):
            break
        # Reach back into the chunk just emitted, then forward to the next word
        # boundary. Guarded to always advance: with a hard-cut chunk shorter than
        # the overlap, stepping back would loop on the same offset forever.
        step_back = max(end - overlap, begin + 1)
        space = text.find(" ", step_back)
        begin = space + 1 if 0 <= space < end else step_back
    return chunks
