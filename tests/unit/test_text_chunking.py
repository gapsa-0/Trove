"""Cutting a document into the passages that get indexed.

The sizes matter for a reason that is invisible from here: a chunk is what the
text embedder sees, and it has a 512-token window. A chunk that overruns it does
not fail -- the tokenizer truncates and the tail is silently unsearchable -- so
the hard cap is tested rather than trusted.
"""

from __future__ import annotations

import itertools

from trove.text.chunk import chunk_blocks
from trove.text.results import Block

# Real-ish prose: the boundary search only means anything against text that has
# paragraphs, sentences and spaces where you would expect them.
_PARA = (
    "El arrendatario se compromete a abonar la renta dentro de los primeros "
    "cinco dias de cada mes. La falta de pago faculta al arrendador para "
    "resolver el contrato sin necesidad de requerimiento previo. "
)


def _prose(times: int, page: int | None = None) -> list[Block]:
    return [Block(page, _PARA * times)]


def test_a_short_document_is_one_chunk():
    chunks = chunk_blocks(_prose(1))
    assert len(chunks) == 1
    assert chunks[0].ordinal == 0
    assert chunks[0].text.startswith("El arrendatario")


def test_nothing_to_index_produces_no_chunks():
    for blocks in ([], [Block(1, "")], [Block(1, "   \n\n  \t ")]):
        assert chunk_blocks(blocks) == []


def test_no_chunk_exceeds_the_hard_cap():
    chunks = chunk_blocks(_prose(40))
    assert len(chunks) > 1
    assert all(len(c.text) <= 1900 for c in chunks)


def test_chunks_are_numbered_in_order_from_zero():
    chunks = chunk_blocks(_prose(40))
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_chunks_do_not_open_or_close_mid_word():
    """A boundary lands on whitespace, so the first and last tokens of every
    chunk are whole words -- otherwise a search for the word that happened to
    straddle the cut matches neither side."""
    words = {w for c in chunk_blocks(_prose(40)) for w in (c.text.split()[0], c.text.split()[-1])}
    # Whitespace-delimited tokens of the source, punctuation included: ending on
    # "mes." is ending on a whole word, ending on "me" would not be.
    vocabulary = set(_PARA.split())
    assert words <= vocabulary, sorted(words - vocabulary)


def test_consecutive_chunks_overlap():
    """The sentence that straddles a boundary has to be findable from one side or
    the other, so each chunk reaches back into the one before it."""
    chunks = chunk_blocks(_prose(40))
    for earlier, later in itertools.pairwise(chunks):
        tail = earlier.text[-120:]
        assert any(word in later.text for word in tail.split()[:5])


def test_text_with_no_whitespace_at_all_still_splits():
    """A base64 blob or an unspaced script offers no boundary to cut on. Cutting
    mid-token there is the right answer: the alternative is one chunk that
    overruns the embedder's window and loses everything past it."""
    chunks = chunk_blocks([Block(None, "x" * 100_000)])
    assert len(chunks) > 50
    assert all(len(c.text) <= 1900 for c in chunks)
    assert "".join(c.text for c in chunks).count("x") >= 100_000


def test_a_paged_document_attributes_each_chunk_to_its_page():
    blocks = [Block(page, _PARA * 12) for page in (1, 2, 3)]
    chunks = chunk_blocks(blocks)
    assert all(c.page_first is not None and c.page_last is not None for c in chunks)
    assert min(c.page_first for c in chunks) == 1  # type: ignore[type-var]
    assert max(c.page_last for c in chunks) == 3  # type: ignore[type-var]
    # Pages are read in order, so the attribution never goes backwards.
    firsts = [c.page_first for c in chunks]
    assert firsts == sorted(firsts)


def test_a_chunk_spanning_two_short_pages_reports_both():
    """Short pages pack into one chunk, and the result says "pp. 1-2" rather than
    picking one of them."""
    blocks = [Block(1, "Factura numero 4471."), Block(2, "Total a pagar 1.240 euros.")]
    chunks = chunk_blocks(blocks)
    assert len(chunks) == 1
    assert (chunks[0].page_first, chunks[0].page_last) == (1, 2)


def test_a_format_without_pages_claims_no_page():
    """A .txt has no page 1 to report. Saying so is the point: a fabricated page
    number in a result is worse than none."""
    chunks = chunk_blocks([Block(None, _PARA * 12)])
    assert chunks
    assert all(c.page_first is None and c.page_last is None for c in chunks)


def test_empty_blocks_between_real_ones_are_dropped():
    """A blank page contributes nothing and must not shift the page attribution
    of the text around it."""
    blocks = [Block(1, "Primera pagina."), Block(2, "   "), Block(3, "Tercera pagina.")]
    chunks = chunk_blocks(blocks)
    assert len(chunks) == 1
    assert (chunks[0].page_first, chunks[0].page_last) == (1, 3)
    assert "   " not in chunks[0].text


def test_the_whole_document_survives_chunking():
    """Every word of the source appears somewhere in the output. Overlap means
    some appear twice; none may appear zero times."""
    blocks = _prose(40)
    produced = " ".join(c.text for c in chunk_blocks(blocks))
    for word in {w for w in blocks[0].text.split() if len(w) > 3}:
        assert word in produced, word
