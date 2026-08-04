"""Changing the embedder must re-arm the archive by itself, exactly once."""

from __future__ import annotations

import factories

from trove.config import Config
from trove.faces import backend
from trove.faces import migrate_adaface as mig


def _catalog(tmp_path, with_faces=True):
    conn = factories.make_db(tmp_path)
    factories.add_file(conn, file_id=1, rel_path="a.jpg")
    if with_faces:
        factories.add_face(conn, file_id=1, face_id=10, box=(10, 10, 60, 60), det_score=0.9)
        conn.execute(
            """INSERT INTO face_scan(file_id,n_faces,scanned_at)
               VALUES(1,1,'2026-01-01')"""
        )
        # The fused pass always writes both markers together.
        conn.execute(
            """INSERT INTO pet_scan(file_id,n_animals,model_source,scanned_at)
               VALUES(1,0,'yolox','2026-01-01')"""
        )
    conn.commit()
    return conn


def test_both_scan_markers_reset_together(tmp_path):
    """Regression: clearing only face_scan left the Pets card showing a stale
    "scanned" count for files that were in fact queued for re-detection.

    Detection is ONE shared decode per file covering people and animals, so the
    two markers describe the same work and must be reset as a pair.
    """
    cfg = Config()
    conn = _catalog(tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM pet_scan").fetchone()[0] == 1

    mig.run_if_needed(conn, cfg)

    assert conn.execute("SELECT COUNT(*) FROM face_scan").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM pet_scan").fetchone()[0] == 0
    conn.close()


def test_valid_pet_data_is_not_destroyed(tmp_path):
    """Only what the embedder change invalidated is deleted. The pet embedder
    did not change, so its detections stay and the Pets view keeps its contents
    while the re-run proceeds."""
    cfg = Config()
    conn = _catalog(tmp_path)
    factories.add_animal_detection(
        conn, file_id=1, detection_id=1, species="dog", model_source="yolox"
    )
    conn.commit()

    mig.run_if_needed(conn, cfg)

    assert conn.execute("SELECT COUNT(*) FROM animal_detections").fetchone()[0] == 1
    conn.close()


def test_an_archive_from_the_old_embedder_re_arms_itself(tmp_path):
    """The point of the whole hook: no command, no flag, work starts from zero."""
    cfg = Config()
    conn = _catalog(tmp_path)
    assert mig.stored_embedder(conn) is None  # never recorded = old archive

    stats = mig.run_if_needed(conn, cfg)

    assert stats is not None
    assert conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0] == 0
    # face_scan is what the DETECT stage derives its backlog from, so clearing it
    # is what makes the pipeline pick the work up on its own.
    assert conn.execute("SELECT COUNT(*) FROM face_scan").fetchone()[0] == 0
    assert mig.stored_embedder(conn) == backend.EMBEDDER_VERSION
    conn.close()


def test_it_does_not_run_twice(tmp_path):
    """Reopening the app must not wipe a re-extract that is already underway."""
    cfg = Config()
    conn = _catalog(tmp_path)
    assert mig.run_if_needed(conn, cfg) is not None

    # Simulate progress: some faces have been re-extracted with the new model.
    factories.add_face(conn, file_id=1, box=(10, 10, 60, 60), det_score=0.9)
    conn.execute("INSERT INTO face_scan(file_id,n_faces,scanned_at) VALUES(1,1,'2026-02-01')")
    conn.commit()

    assert mig.run_if_needed(conn, cfg) is None, "migration ran a second time"
    assert conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM face_scan").fetchone()[0] == 1
    conn.close()


def test_a_fresh_archive_is_marked_without_a_pointless_migration(tmp_path):
    cfg = Config()
    conn = _catalog(tmp_path, with_faces=False)
    assert mig.run_if_needed(conn, cfg) is None
    assert mig.stored_embedder(conn) == backend.EMBEDDER_VERSION
    # And it stays a no-op on the next open.
    assert mig.run_if_needed(conn, cfg) is None
    conn.close()


def test_bumping_the_embedder_version_re_arms_again(tmp_path, monkeypatch):
    """The marker is the mechanism: a future embedder swap is automatic too."""
    cfg = Config()
    conn = _catalog(tmp_path)
    mig.run_if_needed(conn, cfg)
    factories.add_face(conn, file_id=1, box=(10, 10, 60, 60), det_score=0.9)
    conn.execute("INSERT INTO face_scan(file_id,n_faces,scanned_at) VALUES(1,1,'2026-02-01')")
    conn.commit()

    monkeypatch.setattr(backend, "EMBEDDER_VERSION", "some-future-model-v2")
    assert mig.run_if_needed(conn, cfg) is not None
    assert conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0] == 0
    assert mig.stored_embedder(conn) == "some-future-model-v2"
    conn.close()


def test_the_users_review_answers_survive_the_automatic_wipe(tmp_path):
    cfg = Config()
    conn = _catalog(tmp_path)
    conn.execute("UPDATE faces SET manual_person='Mari' WHERE id=10")
    conn.commit()

    mig.run_if_needed(conn, cfg)
    assert mig.pending(conn) is True

    # The pipeline re-detects the same face, then reattaches.
    factories.add_face(conn, file_id=1, box=(10, 10, 60, 60), det_score=0.9)
    conn.commit()
    mig.reattach(conn, cfg)

    assert conn.execute("SELECT manual_person FROM faces").fetchone()["manual_person"] == "Mari"
    assert mig.pending(conn) is False
    conn.close()
