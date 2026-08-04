"""A detect pass that was asked for one detector must not act for the other.

People and Pets are chosen separately but share one decode pass (ADR 0004), so
the pass has to know which half it is running. Two things go wrong if it does
not, and both are silent:

* **The backlog never settles.** The pending query asks whether each detector
  still owes a file work. Asked about a detector the archive does not run, every
  file stays pending for ever and the scheduler relaunches the stage the moment
  it finishes — the same failure ``db.scan_settled`` exists to prevent for scan.
* **The other detector's rows are destroyed.** A rewrite is wholesale, so a
  face-only pass with no guard deletes the animals an archive found while Pets
  was on. Switching a feature off is meant to stop scheduling work, not to throw
  away work already done.
"""

from __future__ import annotations

import numpy as np
import pytest
from factories import add_file, make_db

from trove.config import Config
from trove.detect import persist
from trove.detect.extract import pending_count
from trove.detect.results import BOTH_DETECTORS, FACE, PET, FileResult
from trove.services.pending import detect_pending


@pytest.fixture
def catalog_with_one_photo(tmp_path):
    conn = make_db(tmp_path)
    fid = add_file(conn, rel_path="a.jpg", sha256="abc")
    conn.commit()
    return conn, fid, str(tmp_path / "archive.db")


def _animal(species="dog"):
    class _A:
        pass

    a = _A()
    a.species, a.x, a.y, a.w, a.h, a.score = species, 1, 2, 30, 40, 0.9
    a.embedding = np.zeros(8, dtype=np.float32)
    return a


def test_a_face_only_archive_settles_once_its_faces_are_scanned(catalog_with_one_photo):
    conn, fid, db_path = catalog_with_one_photo
    cfg = Config()
    persist.write_scan_markers(conn, fid, FileResult(), "abc", "yolox-v1", "now", frozenset({FACE}))
    conn.commit()

    assert pending_count(conn, cfg, want=frozenset({FACE})) == 0
    assert detect_pending(db_path, None, "yolox-v1", 0, frozenset({FACE})) == 0
    # ... and the same catalogue is still work waiting to be done for an archive
    # that does want pets, which is what makes switching the feature on later
    # pick the whole archive up.
    assert pending_count(conn, cfg, want=BOTH_DETECTORS) == 1
    assert detect_pending(db_path, None, "yolox-v1", 0, frozenset({PET})) == 1


def test_wanting_nothing_is_no_backlog_rather_than_everything(catalog_with_one_photo):
    conn, _fid, db_path = catalog_with_one_photo
    assert pending_count(conn, Config(), want=frozenset()) == 0
    assert detect_pending(db_path, None, "yolox-v1", 0, frozenset()) == 0


def test_a_face_only_rewrite_leaves_the_animals_alone(catalog_with_one_photo):
    """The case that decides whether "switch Pets off" is safe."""
    conn, fid, _db = catalog_with_one_photo
    with_animal = FileResult(animal_hits=[(_animal(), None)])
    persist.rewrite_file_detections(conn, fid, "then", with_animal, "yolox-v1", None, "abc")
    assert conn.execute("SELECT COUNT(*) FROM animal_detections").fetchone()[0] == 1

    persist.rewrite_file_detections(
        conn, fid, "now", FileResult(), "yolox-v1", None, "abc", frozenset({FACE})
    )
    assert conn.execute("SELECT COUNT(*) FROM animal_detections").fetchone()[0] == 1


def test_a_pet_only_rewrite_leaves_the_faces_alone(catalog_with_one_photo):
    conn, fid, _db = catalog_with_one_photo
    conn.execute(
        """INSERT INTO faces(file_id, box_x, box_y, box_w, box_h, det_score, embedding, created_at)
           VALUES(?,1,2,30,40,0.9,?, 'then')""",
        (fid, np.zeros(8, dtype=np.float32).tobytes()),
    )
    persist.rewrite_file_detections(
        conn, fid, "now", FileResult(), "yolox-v1", None, "abc", frozenset({PET})
    )
    assert conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0] == 1


def test_a_detector_that_did_not_run_leaves_no_scan_marker(catalog_with_one_photo):
    """A marker claims "this file was looked at". Writing one for a detector
    that never ran is what would make switching Pets on later find the whole
    archive already scanned, with no animals, for ever."""
    conn, fid, _db = catalog_with_one_photo
    persist.write_scan_markers(conn, fid, FileResult(), "abc", "yolox-v1", "now", frozenset({FACE}))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM face_scan").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM pet_scan").fetchone()[0] == 0
