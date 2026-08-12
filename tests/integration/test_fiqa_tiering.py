"""The Phase-1 quality gate: scoring, tiering, and calibration stability."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from trove.config import Config
from trove.db import database as db
from trove.faces import fiqa


def _conn(tmp_path):
    conn = db.connect(tmp_path / "archive.db")
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/r','2026-01-01')")
    conn.execute(
        """INSERT INTO files(id,root_id,rel_path,size,mtime,media_type,
                             first_seen,last_seen)
           VALUES(1,1,'a.jpg',1,0,'image','2026-01-01','2026-01-01')"""
    )
    return conn


def _add_face(conn, norm, fid=1):
    cur = conn.execute(
        """INSERT INTO faces(file_id,box_x,box_y,box_w,box_h,det_score,
                             fiqa_norm,embedding,created_at)
           VALUES(?,0,0,60,60,0.9,?,X'00',' 2026-01-01')""",
        (fid, norm),
    )
    return cur.lastrowid


def test_tier_boundaries_are_inclusive_at_high_and_exclusive_at_low():
    a = fiqa.QualityAssessor(high=0.55, low=0.30)
    assert a.tier(0.55) == fiqa.HIGH
    assert a.tier(0.9) == fiqa.HIGH
    assert a.tier(0.54) == fiqa.BORDERLINE
    assert a.tier(0.30) == fiqa.BORDERLINE
    assert a.tier(0.29) == fiqa.LOW_QUALITY
    assert a.tier(0.0) == fiqa.LOW_QUALITY


def test_a_bigger_feature_norm_scores_higher():
    """The whole premise: AdaFace's ||z|| tracks image quality."""
    cal = fiqa.Calibration(model="m", mean=20.0, std=5.0, n_faces=100)
    a = fiqa.AdaFaceNormFIQA(cal, h=0.33, high=0.55, low=0.30)
    scores = [a.score_norm(n) for n in (5.0, 15.0, 20.0, 25.0, 40.0)]
    assert scores == sorted(scores)
    assert scores[0] == 0.0 and scores[-1] == 1.0  # clipped at both ends
    assert a.score_norm(20.0) == pytest.approx(0.5)  # the mean sits mid-scale


def test_a_degenerate_calibration_does_not_fling_faces_into_tiers():
    """Zero spread must not divide by ~0 and tier everything at an extreme."""
    cal = fiqa.Calibration(model="m", mean=20.0, std=0.0, n_faces=100)
    a = fiqa.AdaFaceNormFIQA(cal, h=0.33, high=0.55, low=0.30)
    assert a.tier(a.score_norm(1.0)) == fiqa.BORDERLINE
    assert a.tier(a.score_norm(99.0)) == fiqa.BORDERLINE


def test_calibration_is_persisted_so_tiering_does_not_depend_on_batching(tmp_path):
    """A face's tier must not depend on which incremental batch it arrived in.

    The regression this guards: computing mean/std per batch would give the same
    face different scores on different runs, and silently re-tier the archive.
    """
    cfg = Config()
    cfg.faces_fiqa_calib_sample = 10
    conn = _conn(tmp_path)

    for norm in range(10, 20):
        _add_face(conn, float(norm))
    first = fiqa.bootstrap_calibration(conn, cfg)
    assert first is not None
    tiers_after_first = {
        r["id"]: r["quality_tier"] for r in conn.execute("SELECT id, quality_tier FROM faces")
    }

    # A later batch of wildly different norms must NOT move the calibration.
    for norm in range(1000, 1010):
        _add_face(conn, float(norm))
    second = fiqa.bootstrap_calibration(conn, cfg)
    assert (second.mean, second.std) == (first.mean, first.std)

    fiqa.retier_all(conn, cfg)
    still = {r["id"]: r["quality_tier"] for r in conn.execute("SELECT id, quality_tier FROM faces")}
    for fid, tier in tiers_after_first.items():
        assert still[fid] == tier, "an existing face was re-tiered by a later batch"
    conn.close()


def test_retiering_reads_the_stored_norm_and_never_needs_reembedding(tmp_path):
    cfg = Config()
    cfg.faces_fiqa_calib_sample = 10
    conn = _conn(tmp_path)
    for norm in range(10, 30):
        _add_face(conn, float(norm))
    fiqa.bootstrap_calibration(conn, cfg)

    total = conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0]

    # Moving the gate re-tiers the archive from stored norms alone — no image is
    # touched and no embedding recomputed.
    cfg.faces_fiqa_floor_norm, cfg.faces_fiqa_high = 99.0, 1.02  # nothing is good enough
    counts = fiqa.retier_all(conn, cfg)
    assert counts[fiqa.LOW_QUALITY] == total
    assert (
        conn.execute("SELECT COUNT(*) FROM faces WHERE quality_tier='LOW_QUALITY'").fetchone()[0]
        == total
    )

    cfg.faces_fiqa_floor_norm, cfg.faces_fiqa_high = 0.0, 0.0  # everything is
    counts = fiqa.retier_all(conn, cfg)
    assert counts[fiqa.HIGH] == total
    assert counts[fiqa.LOW_QUALITY] == 0
    conn.close()


def test_the_discard_line_is_the_raw_norm_not_the_archive_it_sits_in(tmp_path):
    """A face is discarded for being unusable, not for being in a good archive.

    The regression: LOW_QUALITY was `score < faces_fiqa_low`, and the score is a
    z-score of the norm — so the gate was a percentile and threw away a fixed
    ~10% of every archive however good it was. The same face must keep its tier
    when the population around it improves.
    """
    cfg = Config()
    cfg.faces_fiqa_calib_sample = 10
    cfg.faces_fiqa_floor_norm = 16.0
    conn = _conn(tmp_path)
    kept = _add_face(conn, 18.0)  # usable, but the worst face in either archive
    for norm in (19.0, 20.0, 21.0, 22.0, 23.0, 24.0, 25.0, 26.0, 27.0):
        _add_face(conn, norm)
    fiqa.bootstrap_calibration(conn, cfg)
    tier_of = lambda fid: conn.execute(  # noqa: E731 - one expression, used twice
        "SELECT quality_tier FROM faces WHERE id=?", (fid,)
    ).fetchone()["quality_tier"]
    assert tier_of(kept) != fiqa.LOW_QUALITY

    # Re-calibrate against a much stronger population. Our face now scores under
    # `faces_fiqa_low` — the old gate would have thrown it away for the company
    # it keeps — but it is the same face and stays clusterable.
    for norm in range(40, 60):
        _add_face(conn, float(norm))
    fiqa.recalibrate(conn, cfg)
    assert fiqa.make_assessor(conn, cfg).score(SimpleNamespace(fiqa_norm=18.0)) < cfg.faces_fiqa_low
    assert tier_of(kept) != fiqa.LOW_QUALITY

    # And the floor still discards: below it, nothing else can save a face.
    below = _add_face(conn, 15.9)
    fiqa.retier_all(conn, cfg)
    assert tier_of(below) == fiqa.LOW_QUALITY
    conn.close()


def test_faces_with_no_norm_fall_back_to_the_composite_scorer(tmp_path):
    """Rows from before the gate existed still get tiered, from local metrics."""
    cfg = Config()
    cfg.faces_fiqa_calib_sample = 2
    conn = _conn(tmp_path)
    _add_face(conn, 20.0)
    _add_face(conn, 22.0)
    fiqa.bootstrap_calibration(conn, cfg)
    conn.execute(
        """INSERT INTO faces(file_id,box_x,box_y,box_w,box_h,det_score,
                             quality_score,clipped_fraction,embedding,created_at)
           VALUES(1,0,0,200,200,0.99,0.9,0.0,X'00','2026-01-01')"""
    )
    fiqa.retier_all(conn, cfg)
    row = conn.execute(
        "SELECT fiqa_source, fiqa_score, quality_tier FROM faces WHERE fiqa_norm IS NULL"
    ).fetchone()
    assert row["fiqa_source"] == "composite-v1"
    # A big, sharp, unclipped, confidently-detected face. `in TIERS` was the old
    # assertion and it passes on a score of 0.0 too, which is exactly what the
    # scorer used to produce here -- see the regression test below.
    assert row["quality_tier"] == fiqa.HIGH
    conn.close()


def test_the_composite_scorer_reads_a_stored_row_not_just_a_live_face(tmp_path):
    """retier_all scores sqlite3.Row objects, which have no attributes at all.

    The scorer read its inputs with ``getattr``, so every stored row fell through
    to the defaults and scored 0.0 -- tiering every pre-AdaFace face LOW_QUALITY
    however good it was, and making retier_all's ``det_score AS score`` aliases
    dead. A pristine face and a terrible one must not score the same.
    """
    conn = _conn(tmp_path)
    scorer = fiqa.CompositeFIQA(high=0.55, low=0.30)
    columns = (
        "SELECT det_score AS score, quality_score, clipped_fraction, "
        "box_w AS w, box_h AS h FROM faces WHERE id=?"
    )

    def _insert(det, quality, clipped, side):
        cur = conn.execute(
            """INSERT INTO faces(file_id,box_x,box_y,box_w,box_h,det_score,
                                 quality_score,clipped_fraction,embedding,created_at)
               VALUES(1,0,0,?,?,?,?,?,X'00','2026-01-01')""",
            (side, side, det, quality, clipped),
        )
        return conn.execute(columns, (cur.lastrowid,)).fetchone()

    good = _insert(0.99, 0.9, 0.0, 200)
    poor = _insert(0.31, 0.05, 0.6, 52)

    assert scorer.score(good) > scorer.score(poor)
    assert scorer.tier(scorer.score(good)) == fiqa.HIGH
    # And the same row read as an object must agree: both shapes reach here.
    # zip, not `k in good`: iterating a sqlite3.Row yields its values.
    as_object = SimpleNamespace(**dict(zip(good.keys(), good, strict=True)))
    assert scorer.score(as_object) == scorer.score(good)
    conn.close()


def test_the_uncalibrated_assessor_commits_to_nothing(tmp_path):
    """Before calibration exists, no face may be discarded as LOW_QUALITY."""
    conn = _conn(tmp_path)
    assessor = fiqa.make_assessor(conn, Config())
    assert isinstance(assessor, fiqa.UncalibratedFIQA)
    face = SimpleNamespace(
        fiqa_norm=0.0, score=0.1, quality_score=0.0, clipped_fraction=0.9, w=51, h=51
    )
    assert assessor.tier(assessor.score(face)) == fiqa.BORDERLINE
    conn.close()
