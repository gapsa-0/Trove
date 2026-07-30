import struct

import pytest

from organize_archive.db import database as db
from organize_archive.web import queries, semantic

np = pytest.importorskip("numpy")


def _semantic_catalogue(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/photos','2026-01-01')")
    for file_id, vector in (
        (1, (1.0, 0.0)),
        (2, (0.0, 1.0)),
        (3, (0.7, 0.7)),
    ):
        conn.execute(
            """INSERT INTO files(
                   id,root_id,rel_path,size,mtime,media_type,first_seen,last_seen
               ) VALUES(?,1,?,1,0,'image','2026-01-01','2026-01-01')""",
            (file_id, f"{file_id}.jpg"),
        )
        conn.execute(
            """INSERT INTO semantic_embeddings(
                   file_id,source_sha256,model,dimensions,embedding,status,
                   indexer_version,indexed_at
               ) VALUES(?,'hash','test',2,?,'indexed','test','2026-01-01')""",
            (file_id, struct.pack("<2f", *vector)),
        )
    conn.commit()
    conn.close()
    return db_path


def test_semantic_search_merges_alternate_query_vector(tmp_path):
    db_path = _semantic_catalogue(tmp_path)

    original_only = queries.semantic_search(db_path, [1.0, 0.0], root_id=1, min_similarity=0.8)
    expanded = queries.semantic_search(
        db_path,
        [1.0, 0.0],
        root_id=1,
        min_similarity=0.8,
        alternate_vectors=[([0.0, 1.0], 0.01)],
    )

    assert [item["id"] for item in original_only["items"]] == [1]
    assert [item["id"] for item in expanded["items"]] == [1, 2]
    assert expanded["items"][1]["score"] == 0.99


def test_embed_queries_uses_one_forward_pass(monkeypatch):
    """Both formulations of a search go through the text tower together.

    The original wording and its local translation are embedded in a single
    call, not one apiece: the tower is ~283 MB of int8 weights and the second
    row is nearly free once it is loaded.
    """
    calls = []

    class FakeBackend:
        def embed_texts(self, texts):
            calls.append(list(texts))
            return np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)

    monkeypatch.setattr(semantic, "backend", lambda cfg, log=None: FakeBackend())

    assert semantic.embed_queries(object(), ["lago", "lake"]) == [[1.0, 0.0], [0.0, 1.0]]
    assert calls == [["lago", "lake"]]
