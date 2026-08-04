"""A face inside an animal is suppressed, and a human review can restore it.

This behaviour used to be asserted against ``faces/extract.py`` in
``tests/integration/test_pets.py``. The fused detector (ADR 0004) moved the
animal-overlap veto into ``detect/extract.py``, which runs both detectors over
**one** decode, so that test was left xfailed pointing at a path the app no
longer takes. This is the port.

Two things make it more work than a rename, and are why the fakes below look
the way they do:

* the fused pass decodes the image **itself** and hands the array to both
  detectors, so the fixture needs a genuinely decodable JPEG rather than a few
  bytes standing in for one;
* the backend interface is different. ``faces/extract`` called
  ``process_path_report(path)``; the fused pass calls ``detect_report(img,
  scale)`` and ``probe_faces(img)`` on the face side, ``detect_with_humans(img)``
  and ``detect_humans(img)`` on the pet side, and attaches an ``assessor``.

The veto itself is ``_detect_frame``: a face whose overlap with an animal box
clears ``pets_face_overlap``, and which is *not* also inside a person box, is
counted as ``nonhuman`` and kept out of ``faces``.
"""

from __future__ import annotations

from types import SimpleNamespace

import factories
import pytest

from organize_archive.config import Config
from organize_archive.db import database as db
from organize_archive.detect import extract as detect_extract
from organize_archive.faces import backend as face_backend
from organize_archive.pets import backend as pet_backend
from organize_archive.services import pets_edit

np = pytest.importorskip("numpy")
Image = pytest.importorskip("PIL.Image")
# frame.load_bgr decodes through OpenCV, and extract() catches the resulting
# ImportError per file and records it as a detection error -- so without cv2
# these tests do not raise, they quietly assert against a run that detected
# nothing. Skipping is the honest outcome.
pytest.importorskip("cv2")

# One animal box covering the frame, and one face well inside it: the face's
# overlap with the animal is 1.0, so it clears any pets_face_overlap below that
# and is vetoed unless a person box covers it too.
_ANIMAL_BOX = (0, 0, 200, 200)
_FACE_BOX = (60, 60, 40, 40)


def _catalog(tmp_path):
    """One catalogued file with a real, decodable JPEG behind it.

    ``b"fake"`` is enough for the old per-path backends, which never opened the
    file. The fused pass decodes before it detects, so a file that does not
    decode is simply skipped and the test would pass for the wrong reason.
    """
    conn = factories.make_db(tmp_path)
    (file_id,) = factories.add_files(conn, 1)
    path = tmp_path / "photos" / f"{file_id}.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (200, 200), (120, 90, 60)).save(path, "JPEG")
    conn.commit()
    return conn, file_id


class _PetBackend:
    """One dog covering the frame, and no person anywhere."""

    def detect_with_humans(self, img):
        x, y, w, h = _ANIMAL_BOX
        animal = pet_backend.AnimalDetection(
            species="dog",
            x=x,
            y=y,
            w=w,
            h=h,
            score=0.93,
            embedding=np.ones(384, dtype="float32") / np.sqrt(384),
        )
        return [animal], []

    def detect_humans(self, img):
        return []


class _FaceBackend:
    """One face, inside the dog. ``assessor`` is set by the pass itself."""

    assessor = None

    def detect_report(self, img, scale):
        x, y, w, h = _FACE_BOX
        face = SimpleNamespace(
            x=x,
            y=y,
            w=w,
            h=h,
            score=0.95,
            focus_score=100.0,
            brightness=120.0,
            extreme_fraction=0.01,
            clipped_fraction=0.0,
            quality_score=0.8,
            quality_source="test",
            quality_tier="HIGH",
            fiqa_norm=None,
            fiqa_score=None,
            fiqa_source=None,
            embedding=np.array([1.0, 0.0], dtype="float32"),
        )
        return face_backend.DetectionReport(faces=[face], candidates=1)

    def probe_faces(self, img):
        return [0.95]


@pytest.fixture
def detectors(monkeypatch):
    monkeypatch.setattr(detect_extract, "available", lambda: True)
    return _FaceBackend(), _PetBackend()


def test_a_face_inside_an_animal_is_suppressed_not_stored(tmp_path, detectors):
    conn, _file_id = _catalog(tmp_path)
    face_be, pet_be = detectors
    cfg = Config(pets_face_overlap=0.6)

    stats = detect_extract.extract(conn, cfg, face_be=face_be, pet_be=pet_be)

    assert stats.faces_found == 0
    assert stats.nonhuman_suppressed == 1
    assert conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0] == 0
    candidate = conn.execute("SELECT * FROM nonhuman_detections").fetchone()
    assert candidate is not None, "the suppressed face must stay reviewable"
    assert candidate["kind"] == "animal"
    conn.close()


def test_reviewing_a_suppressed_face_as_human_restores_it(tmp_path, detectors):
    conn, _file_id = _catalog(tmp_path)
    face_be, pet_be = detectors
    cfg = Config(pets_face_overlap=0.6)
    detect_extract.extract(conn, cfg, face_be=face_be, pet_be=pet_be)
    candidate_id = conn.execute("SELECT id FROM nonhuman_detections").fetchone()["id"]
    conn.close()

    result = pets_edit.review_nonhuman(str(tmp_path / "archive.db"), candidate_id, "human")

    assert result["ok"]
    check = db.open_readonly(tmp_path / "archive.db")
    assert check.execute("SELECT COUNT(*) FROM faces").fetchone()[0] == 1
    check.close()


def test_a_rescan_does_not_undo_the_review(tmp_path, detectors, monkeypatch):
    """The correction has to survive the next detect pass over the same file.

    A rescan rewrites this file's detections wholesale
    (``detect/persist.py``), so "the veto runs again and re-suppresses
    the face the user just restored" is the failure this guards. It is also the
    reason the review is stored on the detection rather than derived.
    """
    conn, file_id = _catalog(tmp_path)
    face_be, pet_be = detectors
    cfg = Config(pets_face_overlap=0.6)
    detect_extract.extract(conn, cfg, face_be=face_be, pet_be=pet_be)
    candidate_id = conn.execute("SELECT id FROM nonhuman_detections").fetchone()["id"]
    conn.close()
    pets_edit.review_nonhuman(str(tmp_path / "archive.db"), candidate_id, "human")

    write = db.connect(tmp_path / "archive.db")
    # Force the file back into the backlog the way a detector/config change
    # does, rather than by deleting the scan row: that is the real trigger.
    write.execute("UPDATE pet_scan SET model_source='old-model' WHERE file_id=?", (file_id,))
    write.commit()
    detect_extract.extract(write, cfg, face_be=face_be, pet_be=pet_be)

    assert write.execute("SELECT review_status FROM nonhuman_detections").fetchone()[0] == "human"
    assert write.execute("SELECT COUNT(*) FROM faces").fetchone()[0] == 1
    write.close()
