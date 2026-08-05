"""The text stage end to end: read an archive, and know when to read it again.

The reading itself is covered by the unit tier. What is checked here is the part
that only exists against a real database -- when a file counts as work, when it
stops counting, and what brings it back. Getting that wrong is not a visible
failure: it is an archive that re-reads itself forever, or one that quietly
never reads a file again after the first attempt.
"""

from __future__ import annotations

import logging
import threading

import docfixtures as fx
import pytest

from trove.config import Config
from trove.db import database as db
from trove.pipeline.job import Job, JobContext
from trove.pipeline.runners import text as text_runner
from trove.services import archives as archive_service
from trove.services import documents, text_search
from trove.text.results import DOCUMENTS

WANTED = frozenset({DOCUMENTS})
# "ocr" is not a feature yet, so this stands for whatever the fused pass looks
# like once both halves exist: what matters here is that the key differs.
BOTH = frozenset({DOCUMENTS, "ocr"})

DOCS = {
    "contrato.pdf": None,
    "carta.docx": None,
    "notas.txt": None,
    "escaneo.pdf": None,
}


@pytest.fixture
def archive(tmp_path):
    """A registered archive holding a few real documents, added as the API adds one."""
    source = tmp_path / "src"
    source.mkdir()
    fx.pdf(source / "contrato.pdf", ["Contrato de arrendamiento", "Clausula segunda"])
    fx.docx(source / "carta.docx", ["Estimado cliente, adjuntamos la factura"])
    (source / "notas.txt").write_text("Recordatorio: renovar el seguro", encoding="utf-8")
    fx.scanned_pdf(source / "escaneo.pdf")

    cfg = Config(db_path=str(tmp_path / "legacy.db"), cache_dir=str(tmp_path / "cache"))
    added = archive_service.add_archive(cfg, str(source))
    assert "error" not in added, added
    aid = added["id"]
    cfg.set_archive_features(aid, ["documents"])

    db_path = cfg.archive_db_path(aid)
    conn = db.connect(db_path)
    db.init_db(conn)
    db.reconcile_root(conn, aid, str(source))
    for name in DOCS:
        conn.execute(
            """INSERT INTO files(root_id, rel_path, ext, size, mtime, media_type,
                                 sha256, first_seen, last_seen)
               VALUES(?, ?, ?, ?, 0, 'document', ?, 'now', 'now')""",
            (aid, name, name.rsplit(".", 1)[1], (source / name).stat().st_size, f"sha-{name}"),
        )
    conn.commit()
    conn.close()
    return cfg, aid, db_path, source


def _run(cfg, aid, force=False):
    """Drive the real runner through a real JobContext."""
    job = Job(id=1, kind="text", root_id=aid, root_path="", force=force)
    ctx = JobContext(cfg=cfg, job=job, cancel=threading.Event(), conn=None, log=logging.getLogger())
    text_runner.run(ctx)
    return job


def _rows(db_path):
    conn = db.open_readonly(db_path)
    try:
        return {
            r["rel_path"]: r
            for r in conn.execute(
                "SELECT f.rel_path, t.* FROM doc_text t JOIN files f ON f.id=t.file_id"
            )
        }
    finally:
        conn.close()


def test_a_pass_reads_every_document_and_indexes_its_passages(archive):
    cfg, aid, db_path, _ = archive
    _run(cfg, aid)

    rows = _rows(db_path)
    assert set(rows) == {"contrato.pdf", "carta.docx", "notas.txt", "escaneo.pdf"}
    assert rows["contrato.pdf"]["status"] == "extracted"
    assert rows["contrato.pdf"]["pages"] == 2
    assert rows["carta.docx"]["status"] == "extracted"

    conn = db.open_readonly(db_path)
    try:
        found = conn.execute(
            "SELECT COUNT(*) FROM doc_chunk_fts WHERE doc_chunk_fts MATCH 'arrendamiento'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert found == 1


def test_a_scan_is_recorded_as_skipped_rather_than_failed(archive):
    """An archive of scans running Documents alone must not show an error count.
    Nothing is wrong; the files need the other half of the pass."""
    cfg, aid, db_path, _ = archive
    _run(cfg, aid)

    row = _rows(db_path)["escaneo.pdf"]
    assert row["status"] == "skipped"
    assert row["n_chunks"] == 0
    assert "no text layer" in row["error"]


def test_running_again_changes_nothing(archive):
    """The whole point of the outcome row. Without it every unreadable file would
    be re-derived on every pass, forever."""
    cfg, aid, db_path, _ = archive
    _run(cfg, aid)
    first = {k: dict(v) for k, v in _rows(db_path).items()}

    assert documents.text_pending(db_path, aid, WANTED) == 0
    job = _run(cfg, aid)
    assert job.message == "every document has been read"
    assert {k: dict(v) for k, v in _rows(db_path).items()} == first


# --- the four ways a file becomes work again --------------------------------


def test_changed_bytes_make_a_file_pending_again(archive):
    cfg, aid, db_path, _ = archive
    _run(cfg, aid)
    conn = db.connect(db_path)
    conn.execute("UPDATE files SET sha256='changed' WHERE rel_path='notas.txt'")
    conn.commit()
    conn.close()

    assert documents.text_pending(db_path, aid, WANTED) == 1


def test_a_new_text_version_re_reads_the_whole_archive(archive, monkeypatch):
    """Bumping the version is how a reader or chunker change takes effect. It
    needs no migration precisely because it is asked as a predicate."""
    cfg, aid, db_path, _ = archive
    _run(cfg, aid)
    assert documents.text_pending(db_path, aid, WANTED) == 0

    monkeypatch.setattr(documents, "TEXT_VERSION", "doctext-v2")
    assert documents.text_pending(db_path, aid, WANTED) == 4


def test_switching_the_other_half_on_brings_the_scans_back(archive):
    """The leg that only a fused stage needs, and the one whose absence would be
    invisible: the scanned PDF carries a current hash and a current version, so
    without ``wanted`` on the row it would never be read again."""
    cfg, aid, db_path, _ = archive
    _run(cfg, aid)
    assert documents.text_pending(db_path, aid, WANTED) == 0

    # Text in images switched on: every file was read under a different set of
    # halves, so every file is owed another look.
    assert documents.text_pending(db_path, aid, BOTH) == 4


def test_a_file_never_read_is_pending_from_the_start(archive):
    _cfg, aid, db_path, _ = archive
    assert documents.text_pending(db_path, aid, WANTED) == 4


# --- what the pass must not touch -------------------------------------------


def test_a_hidden_duplicate_is_never_read(archive):
    """Only the copy Browse shows. A document held three times is read once."""
    cfg, aid, db_path, _ = archive
    conn = db.connect(db_path)
    conn.execute("UPDATE files SET hidden=1 WHERE rel_path='carta.docx'")
    conn.commit()
    conn.close()

    _run(cfg, aid)
    assert "carta.docx" not in _rows(db_path)


def test_re_reading_replaces_a_files_passages_rather_than_adding_to_them(archive):
    """Chunk boundaries move when text changes, so merging generations would
    leave passages in the index that exist nowhere in the document."""
    cfg, aid, db_path, source = archive
    _run(cfg, aid)

    conn = db.open_readonly(db_path)
    before = conn.execute("SELECT COUNT(*) FROM doc_chunks").fetchone()[0]
    conn.close()

    (source / "notas.txt").write_text("Texto completamente distinto", encoding="utf-8")
    conn = db.connect(db_path)
    conn.execute("UPDATE files SET sha256='changed' WHERE rel_path='notas.txt'")
    conn.commit()
    conn.close()
    _run(cfg, aid)

    conn = db.open_readonly(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM doc_chunks").fetchone()[0] == before
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM doc_chunk_fts WHERE doc_chunk_fts MATCH 'Recordatorio'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM doc_chunk_fts WHERE doc_chunk_fts MATCH 'distinto'"
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_the_index_never_outlives_the_chunks_it_addresses(archive):
    """A stale rowid in the index addresses whatever row later takes that id.
    Every path that drops chunks has to drop its index rows in the same breath."""
    cfg, aid, db_path, _ = archive
    _run(cfg, aid)

    conn = db.connect(db_path)
    try:
        chunks = conn.execute("SELECT COUNT(*) FROM doc_chunks").fetchone()[0]
        indexed = conn.execute("SELECT COUNT(*) FROM doc_chunk_fts").fetchone()[0]
        assert chunks == indexed > 0

        file_id = conn.execute("SELECT id FROM files WHERE rel_path='contrato.pdf'").fetchone()[0]
        documents.clear_chunks(conn, file_id)
        conn.commit()

        assert (
            conn.execute("SELECT COUNT(*) FROM doc_chunks WHERE file_id=?", (file_id,)).fetchone()[
                0
            ]
            == 0
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM doc_chunk_fts").fetchone()[0]
            == conn.execute("SELECT COUNT(*) FROM doc_chunks").fetchone()[0]
        )
    finally:
        conn.close()


# --- reading pictures, once Text in images is on ----------------------------

needs_ocr = pytest.mark.skipif(
    not __import__("trove.text.ocr", fromlist=["ocr"]).available(),
    reason="the 'ocr' extra is not installed",
)

BOTH_HALVES = ["documents", "ocr"]


@pytest.fixture
def scanned_archive(tmp_path):
    """An archive holding a born-digital PDF, a scan, and a photograph."""
    source = tmp_path / "src"
    source.mkdir()
    fx.pdf(source / "contrato.pdf", ["Contrato de arrendamiento firmado en marzo"])
    fx.scan_pdf(source / "escaneo.pdf", ["FACTURA 4471\nImporte 850,00 EUR"], dpi=200)
    fx.photo(source / "paisaje.jpg")

    cfg = Config(db_path=str(tmp_path / "legacy.db"), cache_dir=str(tmp_path / "cache"))
    added = archive_service.add_archive(cfg, str(source))
    aid = added["id"]
    cfg.set_archive_features(aid, ["documents"])

    db_path = cfg.archive_db_path(aid)
    conn = db.connect(db_path)
    db.init_db(conn)
    db.reconcile_root(conn, aid, str(source))
    for name, media in (
        ("contrato.pdf", "document"),
        ("escaneo.pdf", "document"),
        ("paisaje.jpg", "image"),
    ):
        conn.execute(
            """INSERT INTO files(root_id, rel_path, ext, size, mtime, media_type,
                                 sha256, first_seen, last_seen)
               VALUES(?, ?, ?, ?, 0, ?, ?, 'now', 'now')""",
            (
                aid,
                name,
                name.rsplit(".", 1)[1],
                (source / name).stat().st_size,
                media,
                f"sha-{name}",
            ),
        )
    conn.commit()
    conn.close()
    return cfg, aid, db_path


@needs_ocr
def test_documents_alone_leaves_the_scan_and_never_sees_the_photo(scanned_archive):
    """Two different reasons for two different files: the scan is skipped with a
    reason that invites the other half, and the photograph is not work at all."""
    cfg, aid, db_path = scanned_archive
    _run(cfg, aid)

    rows = _rows(db_path)
    assert rows["contrato.pdf"]["status"] == "extracted"
    assert rows["escaneo.pdf"]["status"] == "skipped"
    assert "pictures of text" in rows["escaneo.pdf"]["error"]
    assert "paisaje.jpg" not in rows, "a picture is not work until Text in images is on"


@needs_ocr
def test_switching_text_in_images_on_reads_the_scan_and_the_photo(scanned_archive):
    """The `wanted` leg doing its job end to end: files already recorded under
    one half become work again under two."""
    cfg, aid, db_path = scanned_archive
    _run(cfg, aid)
    cfg.set_archive_features(aid, BOTH_HALVES)

    both = frozenset({"documents", "ocr"})
    assert documents.text_pending(db_path, aid, both) == 3
    _run(cfg, aid)

    rows = _rows(db_path)
    assert rows["escaneo.pdf"]["status"] == "extracted"
    assert rows["escaneo.pdf"]["extractor"] == "pdf-ocr"
    assert rows["escaneo.pdf"]["confidence"] is not None
    # The photograph was looked at and holds no writing -- a skip, not an error.
    assert rows["paisaje.jpg"]["status"] == "skipped"
    assert "no writing" in rows["paisaje.jpg"]["error"]


@needs_ocr
def test_text_read_from_a_scan_is_searchable(scanned_archive):
    """The whole point: a word that exists only as pixels becomes findable."""
    cfg, aid, db_path = scanned_archive
    cfg.set_archive_features(aid, BOTH_HALVES)
    _run(cfg, aid)

    page = text_search.text_search(db_path, "factura", root_id=aid)
    assert [i["name"] for i in page["items"]] == ["escaneo.pdf"]


@needs_ocr
def test_a_born_digital_pdf_is_not_re_read_as_pictures(scanned_archive):
    """Its pages carry text and no large image, so the expensive half never
    touches it -- the false positive that would double a run."""
    cfg, aid, db_path = scanned_archive
    cfg.set_archive_features(aid, BOTH_HALVES)
    _run(cfg, aid)

    row = _rows(db_path)["contrato.pdf"]
    assert row["extractor"] == "pdf-text", "read from its text layer, not rasterised"
    assert row["confidence"] is None, "a parser is exact; only OCR carries a confidence"
