"""The Phase-1 quality gate: scoring, tiering, and calibration stability."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from organize_archive.config import Config
from organize_archive.db import database as db
from organize_archive.faces import fiqa


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
    cfg.faces_fiqa_low, cfg.faces_fiqa_high = 1.01, 1.02  # nothing is good enough
    counts = fiqa.retier_all(conn, cfg)
    assert counts[fiqa.LOW_QUALITY] == total
    assert (
        conn.execute("SELECT COUNT(*) FROM faces WHERE quality_tier='LOW_QUALITY'").fetchone()[0]
        == total
    )

    cfg.faces_fiqa_low, cfg.faces_fiqa_high = 0.0, 0.0  # everything is
    counts = fiqa.retier_all(conn, cfg)
    assert counts[fiqa.HIGH] == total
    assert counts[fiqa.LOW_QUALITY] == 0
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
        "SELECT fiqa_source, quality_tier FROM faces WHERE fiqa_norm IS NULL"
    ).fetchone()
    assert row["fiqa_source"] == "composite-v1"
    assert row["quality_tier"] in fiqa.TIERS
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
