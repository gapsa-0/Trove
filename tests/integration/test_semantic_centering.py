"""Modality-gap centering: what it changes, and what it must not.

Image and text embeddings sit in two clusters separated by a near-constant
offset, which squeezes every cosine into a narrow band and leaves an absent
subject able to outscore a present one. Subtracting each modality's own mean
before the cosine collapses that offset. Measured on a 497-file archive, the
41-subject score range widens from 0.046-0.146 to 0.14-0.42 and "a dog" moves
from *below* "the surface of mars" to well above it.

Centering reorders results, so the two relevance cuts are tuned for it being
on (see ``Config.semantic_search_center_embeddings``). These tests pin the
arithmetic underneath that, and pin that passing no centre leaves scoring
exactly as it was.
"""

from __future__ import annotations

import math
import struct

import pytest
from helpers import needs_scoring

from organize_archive.db import database as db
from organize_archive.services import search, semantic

pytestmark = needs_scoring

# Deliberately not unit-spaced around the query: b beats a before centering and
# a beats b after, which is the whole point. c is what pulls the mean off the
# query axis and makes that swap happen.
_VECTORS = [(0.9, 0.436), (0.95, 0.312), (0.2, 0.98)]
_QUERY = [1.0, 0.0]


def _catalogue(tmp_path, vectors=_VECTORS, indexer_version=None):
    """A catalogue holding ``vectors`` as 2-d embeddings, ids from 1."""
    db_path = tmp_path / "archive.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/photos','2026-01-01')")
    for file_id, vector in enumerate(vectors, start=1):
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
               ) VALUES(?,'hash','test',2,?,'indexed',?,'2026-01-01')""",
            (
                file_id,
                struct.pack("<2f", *vector),
                indexer_version or semantic.INDEXER_VERSION,
            ),
        )
    conn.commit()
    conn.close()
    return db_path


def _scores(db_path, center=None):
    page = search.semantic_search(
        db_path, _QUERY, root_id=1, min_similarity=-1.0, relative_floor=0.0, center=center
    )
    return [(item["id"], item["score"]) for item in page["items"]]


def test_no_centre_leaves_scoring_exactly_as_it_was(tmp_path):
    """The uncentered path is the plain cosine, untouched by the new parameter."""
    ranked = _scores(_catalogue(tmp_path))

    assert [file_id for file_id, _score in ranked] == [2, 1, 3]
    # The stored vectors are unit to about 1e-4, so compare at that scale
    # rather than pinning float32 round-trip noise.
    assert dict(ranked)[1] == pytest.approx(0.900, abs=1e-3)
    assert dict(ranked)[2] == pytest.approx(0.950, abs=1e-3)


def test_centering_reorders_on_the_shifted_axis(tmp_path):
    """Subtracting the modality means can flip two results, and here it does.

    Worked by hand: the image mean of the three vectors is about (0.683, 0.576),
    which leaves file 1 at cosine 0.840 to the query and file 2 at 0.711 -- the
    reverse of their uncentered 0.90 and 0.95.
    """
    image_center = [
        sum(v[0] for v in _VECTORS) / len(_VECTORS),
        sum(v[1] for v in _VECTORS) / len(_VECTORS),
    ]
    ranked = _scores(_catalogue(tmp_path), center=(image_center, [0.0, 0.0]))

    assert [file_id for file_id, _score in ranked] == [1, 2, 3]
    assert dict(ranked)[1] == pytest.approx(0.840, abs=1e-3)
    assert dict(ranked)[2] == pytest.approx(0.711, abs=1e-3)


def test_a_text_centre_shifts_the_query_too(tmp_path):
    """Both halves are applied; the query is not scored from the origin."""
    db_path = _catalogue(tmp_path)
    origin_only = _scores(db_path, center=([0.0, 0.0], [0.0, 0.0]))
    both = _scores(db_path, center=([0.0, 0.0], [0.0, 0.5]))

    # Shifting the query off the axis it was on has to move the scores.
    assert origin_only != both
    # ... and the shifted query still points somewhere, so nothing degenerates.
    assert all(score == score for _id, score in both)  # not NaN


def test_a_centre_of_the_wrong_width_is_ignored_rather_than_fatal(tmp_path):
    """A centre from another vector space degrades to uncentered scoring.

    Mirrors how an embedding blob of the wrong width is skipped instead of
    crashing the search: a stale cache is a reason to score plainly, never a
    reason to fail the request.
    """
    db_path = _catalogue(tmp_path)

    assert _scores(db_path, center=([0.0, 0.0, 0.0], [0.0, 0.0, 0.0])) == _scores(db_path)


def test_archive_centre_is_the_mean_of_the_stored_vectors(tmp_path):
    center = search.archive_center(_catalogue(tmp_path), 1)

    assert center is not None
    for axis in (0, 1):
        expected = sum(v[axis] / math.hypot(*v) for v in _VECTORS) / len(_VECTORS)
        assert center[axis] == pytest.approx(expected, abs=1e-5)


def test_an_archive_with_nothing_indexed_has_no_centre(tmp_path):
    """Scoring falls back to uncentered rather than inventing a direction."""
    db_path = tmp_path / "empty.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/photos','2026-01-01')")
    conn.commit()
    conn.close()

    assert search.archive_center(db_path, 1) is None


def test_vectors_from_another_embedder_do_not_move_the_centre(tmp_path):
    """The centre is per vector space, so a stale row must not contribute."""
    db_path = _catalogue(tmp_path, indexer_version="some-older-embedder")

    assert search.archive_center(db_path, 1) is None
