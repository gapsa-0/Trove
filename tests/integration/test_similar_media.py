"""Files that look like this one: the viewer's "Looks like this" section.

A file's own embedding IS a query vector, so this is ``semantic_search`` with
the vector read out of the catalogue instead of typed. What these tests pin is
the part that is not the ranking: that the file is not returned as its own
nearest neighbour, and that "cannot answer" is told apart from "answered, and
nothing resembles it" -- the viewer says different things for the two.
"""

import struct

import pytest

from trove.db import database as db
from trove.services import search

FIXED = "2026-01-01T00:00:00"
pytest.importorskip("numpy")


def _vec(*values):
    return struct.pack(f"<{len(values)}f", *values)


def _catalog(tmp_path, vectors):
    conn = db.connect(tmp_path / "archive.db")
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/photos',?)", (FIXED,))
    for fid, blob in vectors.items():
        conn.execute(
            """INSERT INTO files(id,root_id,rel_path,ext,size,mtime,media_type,
                                 sha256,first_seen,last_seen)
               VALUES(?,1,?,'jpg',1,0,'image',?,?,?)""",
            (fid, f"{fid}.jpg", f"sha{fid}", FIXED, FIXED),
        )
        if blob is None:
            continue
        conn.execute(
            """INSERT INTO semantic_embeddings(file_id,source_sha256,model,dimensions,
                                               embedding,status,indexer_version,indexed_at)
               VALUES(?,?,'m',3,?,'indexed','v1',?)""",
            (fid, f"sha{fid}", blob, FIXED),
        )
    conn.commit()
    conn.close()
    return str(tmp_path / "archive.db")


def test_the_file_is_not_its_own_nearest_neighbour(tmp_path):
    """It matches itself at similarity 1.0, so without dropping it the strip
    would open with the picture you are already looking at."""
    path = _catalog(
        tmp_path,
        {1: _vec(1.0, 0.0, 0.0), 2: _vec(0.9, 0.1, 0.0), 3: _vec(0.0, 1.0, 0.0)},
    )

    page = search.similar_media(path, 1, root_id=1, limit=8)

    assert [item["id"] for item in page["items"]] == [2, 3]


def test_the_closest_picture_comes_first(tmp_path):
    path = _catalog(
        tmp_path,
        {1: _vec(1.0, 0.0, 0.0), 2: _vec(0.0, 1.0, 0.0), 3: _vec(0.95, 0.05, 0.0)},
    )

    page = search.similar_media(path, 1, root_id=1, limit=8)

    assert page["items"][0]["id"] == 3


def test_limit_is_honoured_after_the_file_itself_is_dropped(tmp_path):
    """The query asks for one extra precisely because the file is dropped; a
    limit of 1 must still come back with one neighbour, not zero."""
    path = _catalog(
        tmp_path,
        {1: _vec(1.0, 0.0, 0.0), 2: _vec(0.9, 0.1, 0.0), 3: _vec(0.8, 0.2, 0.0)},
    )

    page = search.similar_media(path, 1, root_id=1, limit=1)

    assert len(page["items"]) == 1
    assert page["count"] == 1


def test_a_file_with_no_embedding_cannot_be_answered_at_all(tmp_path):
    """None, not an empty page: "we have nothing to compare with" and "we
    compared and found nothing" are different, and the panel says different
    things for them."""
    path = _catalog(tmp_path, {1: None, 2: _vec(1.0, 0.0, 0.0)})

    assert search.similar_media(path, 1, root_id=1, limit=8) is None


def test_an_archive_holding_only_this_file_answers_with_nothing(tmp_path):
    path = _catalog(tmp_path, {1: _vec(1.0, 0.0, 0.0)})

    page = search.similar_media(path, 1, root_id=1, limit=8)

    assert page is not None  # it could be answered...
    assert page["items"] == []  # ...and the answer is that nothing resembles it
