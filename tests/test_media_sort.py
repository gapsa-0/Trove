import struct

from organize_archive.db import database as db
from organize_archive.gui import queries


def _dated_catalogue(tmp_path):
    """Three dated files plus one with no date at all, each with an embedding."""
    db_path = tmp_path / "archive.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/photos','2026-01-01')")
    for file_id, taken, vector in (
        (1, "2024-05-01T10:00:00", (1.0, 0.0)),
        (2, "2020-01-01T10:00:00", (1.0, 0.0)),
        (3, "2022-09-15T10:00:00", (1.0, 0.0)),
        (4, None, (1.0, 0.0)),
    ):
        conn.execute(
            """INSERT INTO files(
                   id,root_id,rel_path,size,mtime,media_type,first_seen,last_seen
               ) VALUES(?,1,?,1,0,'image','2026-01-01','2026-01-01')""",
            (file_id, f"{file_id}.jpg"),
        )
        if taken:
            conn.execute(
                "INSERT INTO dates(file_id,best_datetime) VALUES(?,?)",
                (file_id, taken),
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


def test_media_sorts_newest_first_by_default_and_oldest_on_request(tmp_path):
    db_path = _dated_catalogue(tmp_path)

    newest = queries.media(db_path, root_id=1)
    oldest = queries.media(db_path, root_id=1, sort="oldest")

    # The undated file trails both orderings rather than leading "oldest".
    assert [item["id"] for item in newest["items"]] == [1, 3, 2, 4]
    assert [item["id"] for item in oldest["items"]] == [2, 3, 1, 4]


def test_semantic_search_can_be_reordered_by_date(tmp_path):
    db_path = _dated_catalogue(tmp_path)

    newest = queries.semantic_search(
        db_path, [1.0, 0.0], root_id=1, min_similarity=0.5, sort="newest"
    )
    oldest = queries.semantic_search(
        db_path, [1.0, 0.0], root_id=1, min_similarity=0.5, sort="oldest"
    )

    assert [item["id"] for item in newest["items"]] == [1, 3, 2, 4]
    assert [item["id"] for item in oldest["items"]] == [2, 3, 1, 4]


def test_semantic_date_sort_does_not_admit_weak_matches(tmp_path):
    """Sorting reorders the matched set; the similarity floor still applies."""
    db_path = _dated_catalogue(tmp_path)
    conn = db.connect(db_path)
    conn.execute(
        "UPDATE semantic_embeddings SET embedding=? WHERE file_id=1",
        (struct.pack("<2f", 0.0, 1.0),),
    )
    conn.commit()
    conn.close()

    result = queries.semantic_search(
        db_path, [1.0, 0.0], root_id=1, min_similarity=0.5, sort="newest"
    )

    assert [item["id"] for item in result["items"]] == [3, 2, 4]
    assert result["total"] == 3
