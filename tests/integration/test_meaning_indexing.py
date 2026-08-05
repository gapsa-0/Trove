"""The meaning stage end to end: embed an archive's passages, and know when not to.

What only exists against a real database is the bookkeeping — when a document
counts as owed a vector, when it stops counting, and what the text stage doing
its own work underneath does to that. The embedding recipe itself is covered by
``tests/unit/test_e5_embedding.py``.
"""

from __future__ import annotations

import logging
import threading

import pytest

from trove.config import Config
from trove.db import database as db
from trove.embeddings import text_backend as tb
from trove.paths import default_cache_dir
from trove.pipeline.job import Job, JobContext
from trove.pipeline.runners import meaning as meaning_runner
from trove.services import archives as archive_service
from trove.services import meaning

_CACHE = str(default_cache_dir())

pytest.importorskip("numpy")
pytest.importorskip("onnxruntime")

pytestmark = [
    pytest.mark.models,
    pytest.mark.slow,
    pytest.mark.skipif(
        not tb.models_ready(_CACHE),
        reason="the multilingual-e5-small weights are not downloaded on this machine",
    ),
]

PASSAGES = {
    "contrato.pdf": [
        "El importe del alquiler mensual asciende a 850 euros.",
        "El contrato finaliza el 31 de diciembre de 2026.",
    ],
    "garantia.pdf": [
        "La garantia del televisor cubre dos anos desde la compra. "
        "Referencia XB-99231 del fabricante."
    ],
    "recetas.txt": ["Bata los huevos con el azucar y anada la harina tamizada."],
}


@pytest.fixture
def archive(tmp_path):
    """A registered archive whose documents have already been read into passages."""
    source = tmp_path / "src"
    source.mkdir()
    cfg = Config(db_path=str(tmp_path / "legacy.db"), cache_dir=_CACHE)
    added = archive_service.add_archive(cfg, str(source))
    assert "error" not in added, added
    aid = added["id"]
    cfg.set_archive_features(aid, ["documents", "meaning"])

    db_path = cfg.archive_db_path(aid)
    conn = db.connect(db_path)
    db.init_db(conn)
    db.reconcile_root(conn, aid, str(source))
    for name, bodies in PASSAGES.items():
        (source / name).write_text("\n".join(bodies), encoding="utf-8")
        cur = conn.execute(
            """INSERT INTO files(root_id, rel_path, ext, size, mtime, media_type,
                                 sha256, first_seen, last_seen)
               VALUES(?, ?, ?, 10, 0, 'document', ?, 'now', 'now')""",
            (aid, name, name.rsplit(".", 1)[1], f"sha-{name}"),
        )
        fid = cur.lastrowid
        for ordinal, body in enumerate(bodies):
            chunk = conn.execute(
                """INSERT INTO doc_chunks(file_id, ordinal, page_first, page_last, chars)
                   VALUES(?,?,?,?,?)""",
                (fid, ordinal, ordinal + 1, ordinal + 1, len(body)),
            )
            conn.execute(
                "INSERT INTO doc_chunk_fts(rowid, text) VALUES(?, ?)", (chunk.lastrowid, body)
            )
    conn.commit()
    conn.close()
    return cfg, aid, db_path


def _run(cfg, aid, force=False):
    job = Job(id=1, kind="meaning", root_id=aid, root_path="", force=force)
    ctx = JobContext(cfg=cfg, job=job, cancel=threading.Event(), conn=None, log=logging.getLogger())
    meaning_runner.run(ctx)
    return job


def _count(db_path, sql="SELECT COUNT(*) FROM doc_chunk_embeddings"):
    conn = db.open_readonly(db_path)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def test_a_pass_embeds_every_passage(archive):
    cfg, aid, db_path = archive
    assert meaning.meaning_pending(db_path, aid) == 3

    _run(cfg, aid)

    assert _count(db_path) == 4  # two passages in one document, one in each other
    assert meaning.meaning_pending(db_path, aid) == 0


def test_a_stored_vector_has_the_declared_width(archive):
    """The blob is read back by width at search time; a mismatch is skipped
    silently there, so it has to be right when it is written."""
    cfg, aid, db_path = archive
    _run(cfg, aid)
    conn = db.open_readonly(db_path)
    try:
        for row in conn.execute("SELECT embedding, dimensions FROM doc_chunk_embeddings"):
            assert row["dimensions"] == tb.DIMENSIONS
            assert len(row["embedding"]) == tb.DIMENSIONS * 4
    finally:
        conn.close()


def test_running_again_embeds_nothing(archive):
    cfg, aid, db_path = archive
    _run(cfg, aid)
    job = _run(cfg, aid)
    assert job.message == "every document has been indexed for meaning"
    assert _count(db_path) == 4


def test_a_document_is_only_done_when_all_of_it_is(archive):
    """Counting any-vector would call a half-embedded document finished and leave
    the rest of it unsearchable with the card reading complete."""
    cfg, aid, db_path = archive
    _run(cfg, aid)

    conn = db.connect(db_path)
    conn.execute(
        """DELETE FROM doc_chunk_embeddings WHERE chunk_id IN
             (SELECT c.id FROM doc_chunks c JOIN files f ON f.id=c.file_id
               WHERE f.rel_path='contrato.pdf' AND c.ordinal=1)"""
    )
    conn.commit()
    conn.close()

    assert meaning.meaning_pending(db_path, aid) == 1


def test_a_new_embedder_version_re_embeds_the_archive(archive, monkeypatch):
    cfg, aid, db_path = archive
    _run(cfg, aid)
    assert meaning.meaning_pending(db_path, aid) == 0

    monkeypatch.setattr(meaning, "MEANING_VERSION", "e5-small-multilingual-int8-v2")
    assert meaning.meaning_pending(db_path, aid) == 3


def test_re_reading_a_document_takes_its_vectors_with_it(archive):
    """Chunk boundaries move when text changes, so a vector for a passage that no
    longer exists is worse than none. The cascade is what enforces it."""
    cfg, aid, db_path = archive
    _run(cfg, aid)

    conn = db.connect(db_path)
    fid = conn.execute("SELECT id FROM files WHERE rel_path='contrato.pdf'").fetchone()[0]
    from trove.services import documents

    documents.clear_chunks(conn, fid)
    conn.commit()
    conn.close()

    assert _count(db_path) == 2  # the other two documents' passages survive


def test_a_hidden_duplicate_is_never_embedded(archive):
    cfg, aid, db_path = archive
    conn = db.connect(db_path)
    conn.execute("UPDATE files SET hidden=1 WHERE rel_path='recetas.txt'")
    conn.commit()
    conn.close()

    _run(cfg, aid)
    assert _count(db_path) == 3


def test_the_vectors_actually_separate_meaning(archive):
    """The point of the whole stage, asserted against what was stored rather than
    against the model: a question about rent is closer to the rent clause than to
    a recipe, using only the blobs in the database."""
    import numpy as np

    cfg, aid, db_path = archive
    _run(cfg, aid)

    conn = db.open_readonly(db_path)
    try:
        stored = {
            r["rel_path"]: np.frombuffer(r["embedding"], dtype="<f4")
            for r in conn.execute(
                """SELECT f.rel_path, e.embedding FROM doc_chunk_embeddings e
                     JOIN doc_chunks c ON c.id=e.chunk_id
                     JOIN files f ON f.id=c.file_id
                    WHERE c.ordinal=0"""
            )
        }
    finally:
        conn.close()

    query = np.array(meaning.embed_queries(cfg, ["cuanto pago de alquiler"])[0], dtype="<f4")
    assert query @ stored["contrato.pdf"] > query @ stored["recetas.txt"]
    assert query @ stored["contrato.pdf"] > query @ stored["garantia.pdf"]


# --- what fusing the two rankings buys --------------------------------------


def test_meaning_finds_a_document_whose_words_do_not_match(archive):
    """The half that words cannot do. Not one token of this question appears in
    the document it should find -- which is the ordinary case for paperwork
    filed years ago, when you remember what a thing was about and not how it was
    worded."""
    from trove.services import text_search

    cfg, aid, db_path = archive
    _run(cfg, aid)

    query = "cuanto cuesta vivir aqui cada mes"
    assert text_search.text_search(db_path, query, root_id=aid)["total"] == 0

    vector = meaning.embed_queries(cfg, [query])[0]
    fused = text_search.text_search(
        db_path, query, query_vector=vector, root_id=aid, min_similarity=0.70
    )
    assert "contrato.pdf" in [i["name"] for i in fused["items"]]


def test_words_find_a_reference_the_embedder_blurs(archive):
    """The half that meaning cannot do, and the reason both are kept. A model
    that puts "XB-99231" near every other reference number is behaving exactly
    as designed; an index that matches the string is what you want here."""
    from trove.services import text_search

    cfg, aid, db_path = archive
    _run(cfg, aid)

    query = "XB-99231"
    words_only = text_search.text_search(db_path, query, root_id=aid)
    assert [i["name"] for i in words_only["items"]] == ["garantia.pdf"]

    # And fusing must not lose it: it is still first once the vectors join in.
    vector = meaning.embed_queries(cfg, [query])[0]
    fused = text_search.text_search(
        db_path, query, query_vector=vector, root_id=aid, min_similarity=0.70
    )
    assert fused["items"][0]["name"] == "garantia.pdf"


def test_a_document_found_only_by_meaning_still_shows_a_passage(archive):
    """There is no literal match to highlight, so it gets a plain excerpt rather
    than an empty box -- and no marks, because nothing matched literally."""
    from trove.services.text_search import MARK_OPEN, text_search

    cfg, aid, db_path = archive
    _run(cfg, aid)

    vector = meaning.embed_queries(cfg, ["cuanto cuesta vivir aqui"])[0]
    page = text_search(db_path, "", query_vector=vector, root_id=aid, min_similarity=0.70)

    assert page["items"], "the meaning half answers on its own"
    shown = page["items"][0]
    assert shown["snippet"].strip()
    assert MARK_OPEN not in shown["snippet"]


def test_the_meaning_half_alone_answers_when_there_are_no_words_to_match(archive):
    """A query whose every token is punctuation still has a vector, so an
    archive with Search by meaning is not left with nothing."""
    from trove.services.text_search import text_search

    cfg, aid, db_path = archive
    _run(cfg, aid)

    vector = meaning.embed_queries(cfg, ["alquiler"])[0]
    assert text_search(db_path, "!!!", root_id=aid)["total"] == 0
    assert (
        text_search(db_path, "!!!", query_vector=vector, root_id=aid, min_similarity=0.70)["total"]
        > 0
    )
