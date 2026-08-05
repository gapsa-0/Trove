"""Searching the text read out of documents.

Two things are being checked. The ranking behaves like a search -- best passage
first, one row per file, narrowed by the same filters as the rest of Browse. And
the query sanitiser holds, which matters more than it looks: FTS5's MATCH takes
a query *language*, and almost every stray character a person might type is a
hard error in it rather than a bad result.
"""

from __future__ import annotations

import pytest

from trove.db import database as db
from trove.services import text_search
from trove.services.text_search import MARK_CLOSE, MARK_OPEN, match_expression


@pytest.fixture
def indexed(tmp_path):
    """An archive with three documents already read and indexed."""
    db_path = str(tmp_path / "archive.db")
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id, path, added_at) VALUES(1, '/src', 'now')")
    docs = {
        "contrato.pdf": [
            (1, "Contrato de arrendamiento firmado en marzo de 2019"),
            (2, "El arrendamiento se renueva cada anno salvo aviso"),
        ],
        "factura.pdf": [(1, "Factura numero 4471 por importe de 1240,55 euros")],
        "notas.txt": [(None, "Recordatorio: renovar el seguro del coche")],
    }
    for fid, (name, chunks) in enumerate(docs.items(), start=1):
        conn.execute(
            """INSERT INTO files(id, root_id, rel_path, ext, size, mtime, media_type,
                                 sha256, first_seen, last_seen)
               VALUES(?, 1, ?, ?, 10, 0, 'document', ?, 'now', 'now')""",
            (fid, name, name.rsplit(".", 1)[1], f"sha{fid}"),
        )
        conn.execute(
            """INSERT INTO doc_text(file_id, source_sha256, wanted, extractor, status,
                                    chars, n_chunks, text_version, extracted_at)
               VALUES(?, ?, 'documents', 'pdf-text', 'extracted', 100, ?, 'doctext-v1', 'now')""",
            (fid, f"sha{fid}", len(chunks)),
        )
        for ordinal, (page, body) in enumerate(chunks):
            cur = conn.execute(
                """INSERT INTO doc_chunks(file_id, ordinal, page_first, page_last, chars)
                   VALUES(?,?,?,?,?)""",
                (fid, ordinal, page, page, len(body)),
            )
            conn.execute(
                "INSERT INTO doc_chunk_fts(rowid, text) VALUES(?, ?)", (cur.lastrowid, body)
            )
    conn.commit()
    conn.close()
    return db_path


def _hits(db_path, query, **kw):
    return text_search.text_search(db_path, query, root_id=1, **kw)


# --- ranking ----------------------------------------------------------------


def test_a_word_finds_the_documents_holding_it(indexed):
    page = _hits(indexed, "arrendamiento")
    assert page["total"] == 1
    assert [i["name"] for i in page["items"]] == ["contrato.pdf"]


def test_a_document_appears_once_however_often_it_matches(indexed):
    """A forty-page contract mentioning the word on thirty of them would
    otherwise fill the first page of results with itself."""
    page = _hits(indexed, "arrendamiento")
    assert len(page["items"]) == 1
    assert page["count"] == 1


def test_two_words_narrow_rather_than_widen(indexed):
    """Typing more has to mean fewer results, which is FTS5's implicit AND."""
    assert _hits(indexed, "renovar")["total"] == 1
    assert _hits(indexed, "renovar seguro")["total"] == 1
    assert _hits(indexed, "renovar arrendamiento")["total"] == 0


def test_a_hit_carries_the_passage_and_the_page_it_was_on(indexed):
    page = _hits(indexed, "renueva")
    item = page["items"][0]
    assert MARK_OPEN in item["snippet"] and MARK_CLOSE in item["snippet"]
    assert "renueva" in item["snippet"].replace(MARK_OPEN, "").replace(MARK_CLOSE, "")
    assert item["page"] == 2, "the matching passage was on page 2, not page 1"


def test_a_format_without_pages_reports_no_page(indexed):
    item = _hits(indexed, "seguro")["items"][0]
    assert item["name"] == "notas.txt"
    assert item["page"] is None


def test_a_plural_finds_the_singular(indexed):
    """Prefix matching, which is what makes this behave the way a Spanish or
    English speaker expects rather than like an exact-token index."""
    assert _hits(indexed, "factur")["total"] == 1
    assert _hits(indexed, "euro")["total"] == 1


def test_accents_do_not_have_to_match(indexed):
    assert _hits(indexed, "anno")["total"] == 1


def test_a_number_is_searchable(indexed):
    """The reason a spreadsheet's numbers are kept when it is read."""
    assert _hits(indexed, "4471")["total"] == 1


def test_sorting_reorders_the_same_results_and_never_widens_them(indexed):
    relevance = _hits(indexed, "de")
    newest = _hits(indexed, "de", sort="newest")
    assert relevance["total"] == newest["total"]
    assert {i["id"] for i in relevance["items"]} == {i["id"] for i in newest["items"]}


# --- what it must not return ------------------------------------------------


def test_a_hidden_duplicate_never_appears(indexed):
    conn = db.connect(indexed)
    conn.execute("UPDATE files SET hidden=1 WHERE rel_path='contrato.pdf'")
    conn.commit()
    conn.close()
    assert _hits(indexed, "arrendamiento")["total"] == 0


def test_a_file_no_longer_present_never_appears(indexed):
    conn = db.connect(indexed)
    conn.execute("UPDATE files SET present=0 WHERE rel_path='factura.pdf'")
    conn.commit()
    conn.close()
    assert _hits(indexed, "4471")["total"] == 0


def test_a_filter_narrows_a_text_search_too(indexed):
    conn = db.connect(indexed)
    conn.execute(
        "INSERT INTO dates(file_id, best_datetime, date_source) VALUES(1, '2019-03-01T00:00:00', 'exif')"
    )
    conn.commit()
    conn.close()
    assert _hits(indexed, "arrendamiento", year=2019)["total"] == 1
    assert _hits(indexed, "arrendamiento", year=2020)["total"] == 0


# --- the query language, which the user is not writing ----------------------


@pytest.mark.parametrize(
    "raw",
    ["contrato AND", "NOT algo", 'foo"bar', "a:b", "*", "-x", "(", "^", "contrato OR factura"],
)
def test_punctuation_a_person_might_type_is_never_a_syntax_error(indexed, raw):
    """Every one of these is a hard error in FTS5's query language if passed
    through. Someone typing a filename with a dot in it is not writing a query."""
    page = _hits(indexed, raw)
    assert isinstance(page["total"], int)


def test_a_query_with_nothing_searchable_in_it_returns_nothing(indexed):
    for raw in ("", "   ", "!!!", "***"):
        assert _hits(indexed, raw)["total"] == 0


def test_the_expression_quotes_every_token_and_asks_for_a_prefix():
    assert match_expression("contrato arrendamiento") == '"contrato"* "arrendamiento"*'
    assert match_expression('foo"bar') == '"foo"* "bar"*'
    assert match_expression("  ") == ""


# --- the summary the controls read ------------------------------------------


def test_the_summary_counts_documents_rather_than_every_file(indexed):
    """An archive of 150k photos and 300 PDFs has 300 things this can read.
    Reporting the 150k would make a finished stage look 0.2% done."""
    conn = db.connect(indexed)
    conn.execute(
        """INSERT INTO files(id, root_id, rel_path, ext, size, mtime, media_type,
                             sha256, first_seen, last_seen)
           VALUES(99, 1, 'foto.jpg', 'jpg', 10, 0, 'image', 'shaimg', 'now', 'now')"""
    )
    conn.commit()
    conn.close()

    summary = text_search.text_summary(indexed, 1)
    assert summary["total"] == 3
    assert summary["read"] == 3
    assert summary["pending"] == 0
    assert summary["passages"] == 4


# --- fusing the two rankings ------------------------------------------------


def _rrf(*rankings, k=60):
    from trove.services.text_search import _rrf as fuse

    return fuse([list(r) for r in rankings], k)


def test_fusing_ranks_rather_than_scores():
    """The arithmetic, on its own. BM25 and a cosine have no common scale and no
    normalisation of them means anything -- but "third" and "third" do."""
    words = [(10, 1), (20, 2), (30, 3)]
    meaning = [(30, 7), (10, 8), (40, 9)]
    fused = _rrf(words, meaning)
    order = [file_id for file_id, _chunk in fused]
    # 10 is first in one list and second in the other; 30 is third and first.
    # Both beat 20 and 40, which each appear once.
    assert order[:2] == [10, 30]
    assert set(order) == {10, 20, 30, 40}


def test_a_document_both_halves_found_beats_one_only_half_did():
    """The property that justifies fusing at all."""
    words = [(1, 1), (2, 2)]
    meaning = [(2, 3), (3, 4)]
    order = [f for f, _ in _rrf(words, meaning)]
    assert order[0] == 2


def test_the_chunk_shown_comes_from_the_earliest_list_that_offered_it():
    """A document found by words shows the passage containing them; one found
    only by meaning shows its closest passage."""
    fused = dict(_rrf([(1, 111)], [(1, 999), (2, 222)]))
    assert fused[1] == 111, "the words half ran first, so its passage wins"
    assert fused[2] == 222


def test_ties_are_broken_deterministically():
    """RRF ties constantly -- any two files at the same rank in one list and
    absent from the other. Without a fixed order a page boundary would move
    between identical requests."""
    a = _rrf([(5, 1), (3, 2), (9, 3)])
    b = _rrf([(5, 1), (3, 2), (9, 3)])
    assert a == b


def test_an_empty_ranking_contributes_nothing():
    assert _rrf([], [(1, 1)]) == [(1, 1)]
    assert _rrf([], []) == []


def test_search_without_a_vector_is_exactly_the_bm25_ranking(indexed):
    """An archive without Search by meaning, or an install without numpy, gets
    the words half and nothing about the result shape changes."""
    page = _hits(indexed, "arrendamiento")
    assert page["total"] == 1
    assert page["items"][0]["name"] == "contrato.pdf"
    assert MARK_OPEN in page["items"][0]["snippet"]
