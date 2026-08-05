"""The e5 recipe, against the real weights.

This is the load-bearing test of the text embedder, and it exists because every
way of getting this wrong is silent. Drop the asymmetric prefixes, pool over the
padding instead of the mask, or hand an over-long passage to the tokenizer, and
every vector still has the right shape and every search still returns something
— just worse, with nothing anywhere to indicate it.

So the assertions are behavioural rather than numeric. There is no reference
implementation to compare against without pulling torch, and pinning exact
floats would only pin this build's quantisation. What can be pinned is what the
recipe is *for*: a relevant passage beats an irrelevant one, a vector does not
depend on what it was batched with, and the tail of a long passage stays
findable.
"""

from __future__ import annotations

import pytest

from trove.embeddings import text_backend as tb
from trove.paths import default_cache_dir

# Resolved at import: conftest's isolate_app_data points XDG_DATA_HOME at a
# throwaway directory per test, so asking later would find an empty cache and
# skip even on a machine that has the weights.
_CACHE = str(default_cache_dir())

np = pytest.importorskip("numpy")
pytest.importorskip("onnxruntime")
pytest.importorskip("tokenizers")

pytestmark = [
    pytest.mark.models,
    pytest.mark.slow,
    pytest.mark.skipif(
        not tb.models_ready(_CACHE),
        reason="the multilingual-e5-small weights are not downloaded on this machine",
    ),
]

# One session for the module: loading is a second, and every test below reads it
# rather than writing to it.
_BACKEND: tb.E5Backend | None = None


@pytest.fixture(scope="module")
def e5():
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = tb.E5Backend(_CACHE, threads=2)
        _BACKEND.load()
    return _BACKEND


def _cos(a, b) -> float:
    return float(np.array(a) @ np.array(b))


# --- the shape of what comes out --------------------------------------------


def test_a_vector_has_the_declared_width_and_is_unit_length(e5):
    """Both are relied on downstream: the width is what the stored blob is read
    back as, and unit length is what makes a dot product a cosine."""
    vectors = np.array(e5.embed_passages(["Contrato de arrendamiento.", "Factura 4471."]))
    assert vectors.shape == (2, tb.DIMENSIONS)
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-5)


def test_nothing_in_gives_nothing_out(e5):
    assert e5.embed_passages([]) == []
    assert e5.embed_queries([]) == []


# --- the recipe -------------------------------------------------------------


def test_a_relevant_passage_beats_an_irrelevant_one(e5):
    """The whole feature in one assertion. If the prefixes or the pooling were
    wrong this is what would quietly stop being true."""
    query = e5.embed_queries(["cuanto pago de alquiler"])[0]
    relevant, irrelevant = e5.embed_passages(
        [
            "El importe del alquiler mensual asciende a 850 euros.",
            "Receta de tortilla de patatas para cuatro personas.",
        ]
    )
    assert _cos(query, relevant) > _cos(query, irrelevant)


def test_a_query_finds_a_document_in_the_other_language(e5):
    """The reason this model was chosen over an English one, and the reason the
    Spanish-to-English translator does not enter this path at all."""
    query = e5.embed_queries(["when does the lease end"])[0]
    relevant, irrelevant = e5.embed_passages(
        [
            "El contrato de arrendamiento finaliza el 31 de diciembre de 2026.",
            "Fotografias de las vacaciones en la playa.",
        ]
    )
    assert _cos(query, relevant) > _cos(query, irrelevant)


def test_a_passage_is_not_embedded_as_a_query(e5):
    """e5 is trained asymmetrically, so the two prefixes have to produce
    different vectors. Identical ones would mean a prefix was dropped."""
    text = "El contrato de arrendamiento finaliza en diciembre."
    as_passage = e5.embed_passages([text])[0]
    as_query = e5.embed_queries([text])[0]
    assert _cos(as_passage, as_query) < 0.999


def test_a_vector_does_not_depend_on_what_it_was_batched_with(e5):
    """Masking, stated as the property it buys. A short passage batched beside a
    long one is padded to the long one's width; if that padding reached either
    the attention or the mean, the same text would embed differently depending
    on the company it kept, and every vector in the archive would be a function
    of the batch it happened to land in.

    **The threshold is 0.99 rather than 1.0, and the gap is quantisation, not
    slack.** Measured against these same two inputs: full-precision weights give
    exactly 1.000000, the int8 export gives 0.9952, and passing an all-ones mask
    instead of the real one gives 0.848. So the bar sits far below the noise and
    far above the failure it is here to catch.
    """
    text = "Recibo."
    alone = e5.embed_passages([text])[0]
    with_a_long_neighbour = e5.embed_passages(
        [text, "El arrendatario " * 60 + "abonara la renta mensual dentro de los cinco dias."]
    )[0]
    assert _cos(alone, with_a_long_neighbour) > 0.99


# --- the token budget, which is the one this file exists for ----------------


def test_a_passage_past_the_window_is_split_rather_than_truncated(e5):
    """chunk.py sizes passages in characters and provably cannot bound tokens:
    the densest real case measured 628 tokens at 1195 characters where prose was
    262. Handing that to the tokenizer truncates it in silence."""
    dense = "4471,2024-03-11,1240.55,21.0,B12345678,PAGADO\n" * 40
    raw = e5._tokenizer.encode(tb.PASSAGE_PREFIX + dense).ids
    assert len(raw) > tb.MAX_TOKENS, "the fixture must actually exceed the window"

    windows = e5._windows(dense, tb.PASSAGE_PREFIX)
    assert len(windows) > 1
    assert all(len(w) <= tb.MAX_TOKENS for w in windows)
    # Nothing is dropped: the windows account for every token of the body.
    prefix_len = len(e5._tokenizer.encode(tb.PASSAGE_PREFIX).ids)
    covered = sum(len(w) - prefix_len for w in windows)
    assert covered == len(raw) - prefix_len


def test_every_window_carries_the_prefix(e5):
    """Each window is embedded as a passage in its own right, so one missing its
    prefix sits somewhere else in the space and drags the average with it."""
    dense = "4471,2024-03-11,1240.55,21.0,B12345678,PAGADO\n" * 40
    prefix = e5._tokenizer.encode(tb.PASSAGE_PREFIX).ids
    for window in e5._windows(dense, tb.PASSAGE_PREFIX):
        assert window[: len(prefix)] == prefix


def test_the_end_of_a_long_passage_stays_findable(e5):
    """The failure truncation would cause, written as the thing a user would
    notice: a phrase near the end of a long document still matches it."""
    head, tail = "ALQUILER DE OFICINA", "SEGURO DE INCENDIOS"
    filler = "4471,2024-03-11,1240.55,21.0,B12345678,PAGADO\n" * 40
    [vector] = e5.embed_passages([f"{head} {filler} {tail}"])

    unrelated = e5.embed_queries(["fotografias de la playa"])[0]
    for marker in (head, tail):
        query = e5.embed_queries([marker])[0]
        assert _cos(query, vector) > _cos(unrelated, vector), marker


def test_one_vector_comes_back_per_passage_however_long_it_was(e5):
    """doc_chunk_embeddings keys on chunk_id, so a split passage must still
    resolve to exactly one row."""
    passages = ["corto", "4471,2024-03-11,1240.55,21.0,B12345678,PAGADO\n" * 40, "otro corto"]
    vectors = np.array(e5.embed_passages(passages))
    assert vectors.shape == (3, tb.DIMENSIONS)
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-5)
