import struct

from organize_archive.db import database as db
from organize_archive.gui import queries, semantic


def _semantic_catalogue(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.execute(
        "INSERT INTO roots(id,path,added_at) VALUES(1,'/photos','2026-01-01')"
    )
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

    original_only = queries.semantic_search(
        db_path, [1.0, 0.0], root_id=1, min_similarity=0.8
    )
    expanded = queries.semantic_search(
        db_path, [1.0, 0.0], root_id=1, min_similarity=0.8,
        alternate_vectors=[([0.0, 1.0], 0.01)],
    )

    assert [item["id"] for item in original_only["items"]] == [1]
    assert [item["id"] for item in expanded["items"]] == [1, 2]
    assert expanded["items"][1]["score"] == 0.99


def test_embed_queries_uses_one_multimodal_request(monkeypatch):
    calls = []

    def fake_embed(cfg, inputs, *, input_type):
        calls.append((inputs, input_type))
        return [[1.0], [2.0]]

    monkeypatch.setattr(semantic, "_embed", fake_embed)

    assert semantic.embed_queries(object(), ["lago", "lake"], "unused.db") == [
        [1.0], [2.0]
    ]
    assert calls == [([
        {"content": [{"type": "text", "text": "lago"}]},
        {"content": [{"type": "text", "text": "lake"}]},
    ], "query")]
