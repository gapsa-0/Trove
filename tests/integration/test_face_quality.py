from __future__ import annotations

from types import SimpleNamespace

import factories
import pytest

from trove.config import Config
from trove.faces import backend, extract

np = pytest.importorskip("numpy")
pytestmark = pytest.mark.skipif(backend.cv2 is None, reason="OpenCV is an optional face dependency")


def test_quality_metrics_distinguish_detail_blur_and_extreme_exposure():
    checker = ((np.indices((112, 112)).sum(axis=0) % 2) * 255).astype("uint8")
    detailed = np.repeat(checker[:, :, None], 3, axis=2)
    blurred = backend.cv2.GaussianBlur(detailed, (31, 31), 0)
    black = np.zeros_like(detailed)

    sharp_q = backend.measure_face_quality(detailed, min_focus=35)
    blur_q = backend.measure_face_quality(blurred, min_focus=35)
    black_q = backend.measure_face_quality(black, min_focus=35)

    assert sharp_q.focus_score > blur_q.focus_score
    assert black_q.extreme_fraction == 1.0
    assert black_q.quality_score == 0.0


def test_clipped_fraction_is_bounded_and_detects_partial_boxes():
    measure = backend.FaceBackend._clipped_fraction
    assert measure(10, 10, 20, 20, 100, 100) == 0.0
    assert measure(-10, 10, 20, 20, 100, 100) == pytest.approx(0.5)
    assert measure(200, 200, 20, 20, 100, 100) == 1.0
    assert measure(0, 0, 0, 20, 100, 100) == 1.0


def _catalog(tmp_path):
    conn = factories.make_db(tmp_path)
    (tmp_path / "photos" / "one.jpg").write_bytes(b"not decoded by the fake backend")
    factories.add_file(conn, file_id=1, rel_path="one.jpg")
    conn.commit()
    return conn


class _FakeBackend:
    def process_path_report(self, _path, *, apply_quality_gate=True):
        face = SimpleNamespace(
            x=1,
            y=2,
            w=30,
            h=31,
            score=0.95,
            focus_score=80.0,
            brightness=120.0,
            extreme_fraction=0.02,
            clipped_fraction=0.0,
            quality_score=0.71,
            quality_source="test-v1",
            embedding=np.array([1.0, 0.0], dtype="float32"),
        )
        if apply_quality_gate:
            return backend.DetectionReport(
                faces=[face],
                candidates=3,
                rejected={"score": 0, "size": 0, "focus": 1, "exposure": 1, "clipped": 0},
            )
        return backend.DetectionReport(faces=[face], candidates=1)


def test_extraction_persists_quality_and_rejection_diagnostics(tmp_path, monkeypatch):
    conn = _catalog(tmp_path)
    monkeypatch.setattr(backend, "available", lambda: True)

    stats = extract.extract(conn, Config(), be=_FakeBackend())

    face = conn.execute(
        """SELECT focus_score,brightness,extreme_fraction,clipped_fraction,
                  quality_score,quality_source FROM faces"""
    ).fetchone()
    scan = conn.execute("SELECT * FROM face_scan").fetchone()
    assert tuple(face) == (80.0, 120.0, 0.02, 0.0, 0.71, "test-v1")
    assert scan["n_candidates"] == 3
    assert scan["rejected_focus"] == 1
    assert scan["rejected_exposure"] == 1
    assert stats.candidates == 3
    assert stats.rejected_focus == 1
    assert extract.quality_summary(conn)["cluster_noise"] == 1
    conn.close()


def test_calibration_is_read_only(tmp_path):
    conn = _catalog(tmp_path)
    cfg = Config(
        faces_min_focus=100.0,
        faces_max_extreme_fraction=0.8,
        faces_max_clipped_fraction=0.18,
    )

    stats = extract.calibrate_quality(conn, cfg, limit=1, be=_FakeBackend())

    assert stats.processed == 1
    assert stats.rejected_focus == 1
    assert conn.execute("SELECT COUNT(*) FROM face_scan").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0] == 0
    conn.close()
