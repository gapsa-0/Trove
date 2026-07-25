"""The person-box cross-check in the fused detect stage.

YOLOX calls a human who is not vertical in the frame a ``dog`` with real
confidence, so the `person` box from the same forward pass is what arbitrates
between People and Pets. These cover both directions of that rule.
"""

from __future__ import annotations

import pytest

from organize_archive.config import Config
from organize_archive.db import database as db
from organize_archive.detect import extract as dx
from organize_archive.faces import backend as face_backend
from organize_archive.pets import backend as pet_backend

np = pytest.importorskip("numpy")


def _catalog(tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    conn = db.connect(tmp_path / "archive.db")
    db.init_db(conn)
    conn.execute(
        "INSERT INTO roots(id,path,added_at) VALUES(1,?,'2026-01-01')",
        (str(root),))
    (root / "1.jpg").write_bytes(b"fake")
    conn.execute(
        """INSERT INTO files
           (id,root_id,rel_path,size,mtime,media_type,sha256,first_seen,last_seen)
           VALUES(1,1,'1.jpg',4,0,'image','abc','2026-01-01','2026-01-01')""")
    conn.commit()
    return conn


def _animal(species="dog", *, x=0, y=0, w=100, h=100, score=.8):
    return pet_backend.AnimalDetection(
        species=species, x=x, y=y, w=w, h=h, score=score,
        embedding=np.ones(384, dtype="float32") / np.sqrt(384))


def _human(*, x=0, y=0, w=100, h=100, score=.5):
    return pet_backend.HumanDetection(x=x, y=y, w=w, h=h, score=score)


def _face(*, x=30, y=20, w=30, h=30, score=.9):
    from types import SimpleNamespace
    return SimpleNamespace(
        x=x, y=y, w=w, h=h, score=score, focus_score=100.0, brightness=120.0,
        extreme_fraction=.01, clipped_fraction=0.0, quality_score=.8,
        quality_source="test", embedding=np.array([1.0, 0.0], dtype="float32"))


class _PetBackend:
    """Returns fixed boxes; records how many forward passes were asked for.

    ``turn_humans`` are reported already mapped back to the upright frame, which
    is what the stage sees once ``_human_boxes_on_turns`` has un-rotated them.
    """

    def __init__(self, animals, humans, turn_humans=()):
        self.animals, self.humans, self.turn_humans = animals, humans, turn_humans
        self.passes = self.turn_passes = 0

    def detect_with_humans(self, _img):
        self.passes += 1
        return list(self.animals), list(self.humans)

    def detect_humans(self, _img):
        self.turn_passes += 1
        return list(self.turn_humans)


class _FaceBackend:
    def __init__(self, faces):
        self.faces = faces

    def detect_report(self, _img, _scale=1.0):
        return face_backend.DetectionReport(
            faces=list(self.faces), candidates=len(self.faces))


def _run(conn, cfg, pet_be, face_be, monkeypatch):
    monkeypatch.setattr(dx, "available", lambda: True)
    monkeypatch.setattr(
        dx, "_load_bgr", lambda _p, _s: (np.zeros((100, 100, 3), "uint8"), 1.0))
    return dx.extract(conn, cfg, face_be=face_be, pet_be=pet_be)


def test_person_box_over_an_animal_box_means_a_person_not_a_pet(
        tmp_path, monkeypatch):
    """A sideways human read as `dog`: the pet goes, the face stays."""
    conn = _catalog(tmp_path)
    pet_be = _PetBackend([_animal()], [_human(x=2, y=2, w=98, h=98)])
    face_be = _FaceBackend([_face()])

    stats = _run(conn, Config(), pet_be, face_be, monkeypatch)

    assert stats.human_animals_dropped == 1
    assert stats.animals == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM animal_detections").fetchone()[0] == 0
    assert stats.faces_found == 1          # the real face survives
    assert stats.nonhuman_suppressed == 0
    assert pet_be.turn_passes == 0         # nothing left to re-test on the turns
    conn.close()


def test_a_real_animal_still_suppresses_its_own_face(tmp_path, monkeypatch):
    """No person over the box: the SCRFD hit is the animal's face, dropped."""
    conn = _catalog(tmp_path)
    pet_be = _PetBackend([_animal(species="cat")], [])
    face_be = _FaceBackend([_face()])

    stats = _run(conn, Config(), pet_be, face_be, monkeypatch)

    assert stats.animals == 1
    assert stats.faces_found == 0
    assert stats.nonhuman_suppressed == 1
    assert conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0] == 0
    conn.close()


def test_a_person_holding_a_pet_keeps_both(tmp_path, monkeypatch):
    """Containment is not identity: the person's box wraps the cat's, but the
    two boxes are different objects, so the cat stays a pet and the face stays
    a face."""
    conn = _catalog(tmp_path)
    pet_be = _PetBackend(
        [_animal(species="cat", x=40, y=50, w=40, h=40)],
        [_human(x=0, y=0, w=100, h=100, score=.9)])
    face_be = _FaceBackend([_face(x=20, y=5, w=25, h=25)])

    stats = _run(conn, Config(), pet_be, face_be, monkeypatch)

    assert stats.animals == 1
    assert stats.human_animals_dropped == 0
    assert stats.faces_found == 1
    conn.close()


def test_a_face_inside_both_a_person_and_an_animal_box_is_human(
        tmp_path, monkeypatch):
    """The old rule deleted this face: it sits inside a (bogus) animal box. A
    person box over the same face means it is ours, not the animal's."""
    conn = _catalog(tmp_path)
    pet_be = _PetBackend(
        [_animal(x=0, y=0, w=100, h=100)],
        [_human(x=25, y=15, w=45, h=80, score=.4)])   # too tall to be the dog
    face_be = _FaceBackend([_face(x=30, y=20, w=30, h=30)])

    stats = _run(conn, Config(), pet_be, face_be, monkeypatch)

    assert stats.animals == 1              # box shape differs, still a "pet"
    assert stats.faces_found == 1          # but the face is not the pet's
    assert stats.nonhuman_suppressed == 0
    conn.close()


def test_an_animal_box_is_re_tested_on_the_quarter_turns(tmp_path, monkeypatch):
    """Nothing human upright, but the photo is stored sideways: rotating it
    makes YOLOX read the same region as `person`."""
    conn = _catalog(tmp_path)
    # Upright: no person at all. On a quarter turn: a person over the same box.
    pet_be = _PetBackend([_animal()], [], turn_humans=[_human(w=100, h=100)])
    face_be = _FaceBackend([])

    stats = _run(conn, Config(), pet_be, face_be, monkeypatch)

    assert stats.human_animals_dropped == 1
    assert stats.animals == 0
    assert pet_be.turn_passes == 2         # both turns, and only because a box
    conn.close()                           # survived the upright check


@pytest.mark.parametrize("k", [1, 3])
def test_boxes_from_a_quarter_turn_map_back_to_the_upright_frame(k):
    """Round-trip against a real rotation: mark a patch in the upright frame,
    rotate, take the patch's box there, and it must map back onto itself."""
    w, h = 400, 300
    x, y, bw, bh = 310, 40, 50, 80
    img = np.zeros((h, w), "uint8")
    img[y:y + bh, x:x + bw] = 255

    rows, cols = np.nonzero(np.rot90(img, k))
    rotated = _human(x=int(cols.min()), y=int(rows.min()),
                     w=int(cols.max() - cols.min() + 1),
                     h=int(rows.max() - rows.min() + 1))
    (back,) = dx._rotate_boxes_back([rotated], k, w, h)

    assert (back.x, back.y, back.w, back.h) == (x, y, bw, bh)
