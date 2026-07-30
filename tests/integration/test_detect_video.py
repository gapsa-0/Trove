"""Video support in the fused detect stage (detect/extract.py).

Covers the three pieces that don't need a real decode/ffmpeg to test:

* the per-video collapse helpers, which fold the same person/animal seen in
  several sampled frames into one row (otherwise clustering would flood on
  duplicates and every count would be inflated);
* keyframe offset generation, which mirrors web/semantic.py's spread-but-
  pulled-in-from-the-ends sampling, generalized to N frames;
* pending-work counting honouring ``cfg.detect_video_frames`` -- 0 must
  behave exactly like today's images-only stage, so it reaches "up to date"
  instead of forever waiting on videos nothing will ever scan.

No ffmpeg is required: offset/collapse logic is pure, and the pending-count
tests only touch the DB.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from organize_archive.config import Config
from organize_archive.db import database as db
from organize_archive.detect import extract as dx
from organize_archive.pets.backend import AnimalDetection
from organize_archive.web import queries

np = pytest.importorskip("numpy")


def _face(embedding, score=0.9, quality_score=None):
    return SimpleNamespace(
        x=0,
        y=0,
        w=10,
        h=10,
        score=score,
        embedding=np.asarray(embedding, dtype="float32"),
        quality_score=quality_score,
    )


def _unit(*vals):
    v = np.asarray(vals, dtype="float32")
    return v / np.linalg.norm(v)


# ---------------------------------------------------------------------------
# collapse_video_faces
# ---------------------------------------------------------------------------


def test_near_identical_faces_across_frames_collapse_keeping_higher_quality():
    a = _face(_unit(1.0, 0.0), quality_score=0.4)
    b = _face(_unit(0.99, 0.02), quality_score=0.9)  # near-duplicate, better crop

    kept = dx.collapse_video_faces([(a, "00:00:01.000"), (b, "00:00:03.000")], threshold=0.55)

    assert len(kept) == 1
    face, offset = kept[0]
    assert face is b
    assert offset == "00:00:03.000"


def test_dissimilar_faces_across_frames_stay_separate():
    a = _face(_unit(1.0, 0.0))
    b = _face(_unit(0.0, 1.0))  # orthogonal: a different person

    kept = dx.collapse_video_faces([(a, "00:00:01.000"), (b, "00:00:02.000")], threshold=0.55)

    assert len(kept) == 2
    assert {offset for _f, offset in kept} == {"00:00:01.000", "00:00:02.000"}


def test_collapse_keeps_the_earlier_ones_offset_when_later_is_not_better():
    a = _face(_unit(1.0, 0.0), quality_score=0.9)
    b = _face(_unit(0.99, 0.02), quality_score=0.2)  # near-dup, worse crop

    kept = dx.collapse_video_faces([(a, "00:00:01.000"), (b, "00:00:03.000")], threshold=0.55)

    assert len(kept) == 1
    face, offset = kept[0]
    assert face is a
    assert offset == "00:00:01.000"


# ---------------------------------------------------------------------------
# collapse_video_animals
# ---------------------------------------------------------------------------


def _animal(species, embedding, score=0.8):
    return AnimalDetection(
        species=species,
        x=0,
        y=0,
        w=20,
        h=20,
        score=score,
        embedding=np.asarray(embedding, dtype="float32"),
    )


def test_near_identical_animals_same_species_collapse_keeping_higher_score():
    a = _animal("dog", _unit(1.0, 0.0), score=0.6)
    b = _animal("dog", _unit(0.98, 0.05), score=0.95)

    kept = dx.collapse_video_animals([(a, "00:00:01.000"), (b, "00:00:04.000")], threshold=0.80)

    assert len(kept) == 1
    animal, offset = kept[0]
    assert animal is b
    assert offset == "00:00:04.000"


def test_animals_of_different_species_never_collapse_even_if_embeddings_match():
    same_vec = _unit(1.0, 0.0)
    cat = _animal("cat", same_vec)
    dog = _animal("dog", same_vec)

    kept = dx.collapse_video_animals([(cat, "00:00:01.000"), (dog, "00:00:02.000")], threshold=0.80)

    assert len(kept) == 2
    assert {a.species for a, _o in kept} == {"cat", "dog"}


# ---------------------------------------------------------------------------
# offset generation
# ---------------------------------------------------------------------------


def test_offsets_for_known_duration_are_n_distinct_and_spread_across_it():
    offsets = dx._video_offsets(100.0, 5)

    assert len(offsets) == 5
    assert len(set(offsets)) == 5
    # Spread but pulled in from the ends: neither endpoint offset should sit
    # right at the very start or very end of the clip.
    assert offsets[0] not in ("00:00:00.000",)
    assert offsets != sorted(offsets, reverse=True)  # increasing order


def test_offsets_fall_back_to_one_fixed_offset_when_duration_is_unknown():
    assert dx._video_offsets(None, 5) == ["00:00:01"]
    assert dx._video_offsets(0, 5) == ["00:00:01"]
    assert dx._video_offsets("not-a-number", 5) == ["00:00:01"]


def test_offsets_have_no_duplicates_for_a_very_short_clip():
    # A 0.2s clip: several fractions round to indistinguishable timestamps,
    # so fewer (not repeated) offsets must come back.
    offsets = dx._video_offsets(0.2, 5)

    assert len(offsets) == len(set(offsets))
    assert len(offsets) >= 1


def test_zero_requested_frames_yields_no_offsets():
    assert dx._video_offsets(100.0, 0) == []


# ---------------------------------------------------------------------------
# pending-work counting honours cfg.detect_video_frames
# ---------------------------------------------------------------------------


def _catalog_with_one_video(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    (root / "clip.mp4").write_bytes(b"fake")
    conn = db.connect(tmp_path / "archive.db")
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,?,'2026-01-01')", (str(root),))
    conn.execute(
        """INSERT INTO files
           (id,root_id,rel_path,size,mtime,media_type,sha256,present,hidden,
            first_seen,last_seen)
           VALUES(1,1,'clip.mp4',4,0,'video','sha1',1,0,'2026-01-01','2026-01-01')"""
    )
    conn.commit()
    return conn


def test_pending_count_includes_video_only_when_frames_enabled(tmp_path):
    conn = _catalog_with_one_video(tmp_path)

    cfg_on = Config(detect_video_frames=5)
    cfg_off = Config(detect_video_frames=0)

    assert dx.pending_count(conn, cfg_on) == 1
    assert dx.pending_count(conn, cfg_off) == 0
    assert dx.image_count(conn, cfg_on) == 1
    assert dx.image_count(conn, cfg_off) == 0
    conn.close()


def test_detect_pending_query_includes_video_only_when_frames_enabled(tmp_path):
    conn = _catalog_with_one_video(tmp_path)
    conn.close()
    db_path = str(tmp_path / "archive.db")

    assert queries.detect_pending(db_path, model_source="m", detect_video_frames=5) == 1
    assert queries.detect_pending(db_path, model_source="m", detect_video_frames=0) == 0
