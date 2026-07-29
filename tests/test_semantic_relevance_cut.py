"""The relative relevance cut, and why an absolute one is not enough.

Measured on the real archive (see plan/local-semantic-embeddings.md §7): the
local embedder's cosines are compressed and *shift per query* -- the best
"fireworks" match scores 0.097 while the best "a selfie" match scores 0.150, and
a subject the archive does not contain at all still reaches ~0.10. Any single
absolute floor is therefore either too high for one query or useless for
another. The cut that decides relevance is a fraction of the query's own best
score; ``min_similarity`` survives only as a floor against noise.
"""

from __future__ import annotations

import math
import struct

from organize_archive.db import database as db
from organize_archive.gui import queries


def _catalogue(tmp_path, scores):
    """A catalogue whose files sit at chosen cosines from the query (1, 0)."""
    db_path = tmp_path / "archive.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/photos','2026-01-01')")
    for file_id, cosine in enumerate(scores, start=1):
        conn.execute(
            """INSERT INTO files(
                   id,root_id,rel_path,size,mtime,media_type,first_seen,last_seen
               ) VALUES(?,1,?,1,0,'image','2026-01-01','2026-01-01')""",
            (file_id, f"{file_id}.jpg"),
        )
        vector = (cosine, math.sqrt(max(0.0, 1.0 - cosine * cosine)))
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


def test_the_cut_follows_the_querys_own_best_score():
    """Two queries on very different scales, one setting, sane results for both.

    This is the whole argument for a relative cut. A "strong" query whose best
    match is 0.15 and a "weak" one whose best is 0.09 both keep their close
    matches and drop their distant ones -- something no single absolute floor
    does, since 0.09 is simultaneously the *best* result of one query and a
    reject of the other.
    """
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as strong_dir, tempfile.TemporaryDirectory() as weak_dir:
        strong = _catalogue(Path(strong_dir), [0.150, 0.140, 0.125, 0.080, 0.040])
        weak = _catalogue(Path(weak_dir), [0.090, 0.084, 0.075, 0.048, 0.024])

        kwargs = {"root_id": 1, "min_similarity": -1.0, "relative_floor": 0.80}
        strong_hits = queries.semantic_search(strong, [1.0, 0.0], **kwargs)
        weak_hits = queries.semantic_search(weak, [1.0, 0.0], **kwargs)

    # Each keeps exactly its three near-best matches, on both scales.
    assert [i["id"] for i in strong_hits["items"]] == [1, 2, 3]
    assert [i["id"] for i in weak_hits["items"]] == [1, 2, 3]


def test_totals_reflect_the_relative_cut(tmp_path):
    """``total`` drives the result count the GUI shows, so it must be cut too --
    not just the page of items handed back."""
    db_path = _catalogue(tmp_path, [0.15, 0.14, 0.05, 0.04, 0.03])

    hits = queries.semantic_search(
        db_path, [1.0, 0.0], root_id=1, min_similarity=-1.0, relative_floor=0.80
    )

    assert hits["total"] == 2


def test_the_absolute_floor_still_applies_underneath(tmp_path):
    """A query the archive answers *badly* must not have its handful of
    near-random best matches promoted just because they are its best."""
    db_path = _catalogue(tmp_path, [0.030, 0.029, 0.028])

    hits = queries.semantic_search(
        db_path, [1.0, 0.0], root_id=1, min_similarity=0.05, relative_floor=0.80
    )

    assert hits["items"] == [] and hits["total"] == 0


def test_relative_floor_off_by_default_keeps_every_scored_row(tmp_path):
    db_path = _catalogue(tmp_path, [0.15, 0.10, 0.06])

    hits = queries.semantic_search(db_path, [1.0, 0.0], root_id=1, min_similarity=-1.0)

    assert hits["total"] == 3


def test_an_all_negative_result_set_is_not_inverted_by_the_relative_cut(tmp_path):
    """Scaling a negative best score by 0.8 *raises* the bar toward zero.

    Applied blindly that would discard the entire result set of a query whose
    matches are all poor -- the opposite of trimming it -- so the relative cut
    has to stand down when the best score is not positive.
    """
    db_path = _catalogue(tmp_path, [-0.02, -0.05, -0.09])

    hits = queries.semantic_search(
        db_path, [1.0, 0.0], root_id=1, min_similarity=-1.0, relative_floor=0.80
    )

    assert hits["total"] == 3
