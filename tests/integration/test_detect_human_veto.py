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
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,?,'2026-01-01')", (str(root),))
    (root / "1.jpg").write_bytes(b"fake")
    conn.execute(
        """INSERT INTO files
           (id,root_id,rel_path,size,mtime,media_type,sha256,first_seen,last_seen)
           VALUES(1,1,'1.jpg',4,0,'image','abc','2026-01-01','2026-01-01')"""
    )
    conn.commit()
    return conn


def _animal(species="dog", *, x=0, y=0, w=100, h=100, score=0.8):
    return pet_backend.AnimalDetection(
        species=species,
        x=x,
        y=y,
        w=w,
        h=h,
        score=score,
        embedding=np.ones(384, dtype="float32") / np.sqrt(384),
    )


def _human(*, x=0, y=0, w=100, h=100, score=0.5):
    return pet_backend.HumanDetection(x=x, y=y, w=w, h=h, score=score)


def _face(*, x=30, y=20, w=30, h=30, score=0.9):
    from types import SimpleNamespace

    return SimpleNamespace(
        x=x,
        y=y,
        w=w,
        h=h,
        score=score,
        focus_score=100.0,
        brightness=120.0,
        extreme_fraction=0.01,
        clipped_fraction=0.0,
        quality_score=0.8,
        quality_source="test",
        embedding=np.array([1.0, 0.0], dtype="float32"),
    )


# Landscape, and deliberately not square: a stub can tell an upright frame from
# a quarter turn by its shape alone, and only a landscape frame is ever a
# candidate for being turned upright (see _resolve_rotation).
_IMG_H, _IMG_W = 100, 120


class _PetBackend:
    """Returns fixed boxes; records how many forward passes were asked for.

    ``turn_humans`` is what a quarter-turned frame reports, given in that turned
    frame's own coordinates — the stage maps them back itself.
    """

    def __init__(self, animals, humans, turn_humans=(), turned_animals=()):
        self.animals, self.humans, self.turn_humans = animals, humans, turn_humans
        # What a full pass over the *turned* frame reports, for the case where
        # turning is what finally makes the subject legible.
        self.turned_animals = turned_animals
        self.passes = self.turn_passes = self.human_calls = 0

    def detect_with_humans(self, _img):
        self.passes += 1
        if self.passes > 1:
            return list(self.turned_animals), list(self.turn_humans)
        return list(self.animals), list(self.humans)

    def detect_humans(self, img):
        self.human_calls += 1
        if img.shape[:2] == (_IMG_H, _IMG_W):  # upright frame
            return list(self.humans)
        self.turn_passes += 1
        return list(self.turn_humans)


class _FaceBackend:
    """``faces`` upright; ``turned_faces`` once the frame has been rotated.

    ``probe`` maps a clockwise turn to the raw confidences an orientation probe
    would see there, standing in for SCRFD's rotation sensitivity.
    """

    def __init__(self, faces, turned_faces=None, probe=None):
        self.faces = faces
        self.turned_faces = turned_faces
        self.probe = probe or {}
        self.reports = 0
        self.probed = []

    _PROBE_ORDER = (90, 270)

    def probe_faces(self, _img):
        deg = self._PROBE_ORDER[len(self.probed)]
        self.probed.append(deg)
        return list(self.probe.get(deg, ()))

    def detect_report(self, _img, _scale=1.0):
        self.reports += 1
        shown = (
            self.turned_faces if self.reports > 1 and self.turned_faces is not None else self.faces
        )
        return face_backend.DetectionReport(faces=list(shown), candidates=len(shown))


def _run(conn, cfg, pet_be, face_be, monkeypatch, shape=(_IMG_H, _IMG_W)):
    monkeypatch.setattr(dx, "available", lambda: True)
    monkeypatch.setattr(dx, "_load_bgr", lambda _p, _s: (np.zeros((*shape, 3), "uint8"), 1.0))
    return dx.extract(conn, cfg, face_be=face_be, pet_be=pet_be)


def test_person_box_over_an_animal_box_means_a_person_not_a_pet(tmp_path, monkeypatch):
    """A sideways human read as `dog`: the pet goes, the face stays."""
    conn = _catalog(tmp_path)
    pet_be = _PetBackend([_animal()], [_human(x=2, y=2, w=98, h=98)])
    face_be = _FaceBackend([_face()])

    stats = _run(conn, Config(), pet_be, face_be, monkeypatch)

    assert stats.human_animals_dropped == 1
    assert stats.animals == 0
    assert conn.execute("SELECT COUNT(*) FROM animal_detections").fetchone()[0] == 0
    assert stats.faces_found == 1  # the real face survives
    assert stats.nonhuman_suppressed == 0
    assert pet_be.turn_passes == 0  # nothing left to re-test on the turns
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
        [_human(x=0, y=0, w=100, h=100, score=0.9)],
    )
    face_be = _FaceBackend([_face(x=20, y=5, w=25, h=25)])

    stats = _run(conn, Config(), pet_be, face_be, monkeypatch)

    assert stats.animals == 1
    assert stats.human_animals_dropped == 0
    assert stats.faces_found == 1
    conn.close()


def test_a_face_inside_both_a_person_and_an_animal_box_is_human(tmp_path, monkeypatch):
    """The old rule deleted this face: it sits inside a (bogus) animal box. A
    person box over the same face means it is ours, not the animal's."""
    conn = _catalog(tmp_path)
    pet_be = _PetBackend(
        [_animal(x=0, y=0, w=100, h=100)], [_human(x=25, y=15, w=45, h=80, score=0.4)]
    )  # too tall to be the dog
    face_be = _FaceBackend([_face(x=30, y=20, w=30, h=30)])

    stats = _run(conn, Config(), pet_be, face_be, monkeypatch)

    assert stats.animals == 1  # box shape differs, still a "pet"
    assert stats.faces_found == 1  # but the face is not the pet's
    assert stats.nonhuman_suppressed == 0
    conn.close()


def test_an_animal_box_is_re_tested_on_the_quarter_turns(tmp_path, monkeypatch):
    """Nothing human upright, but the photo is stored sideways: rotating it
    makes YOLOX read the same region as `person`."""
    conn = _catalog(tmp_path)
    # Upright: no person at all. On a quarter turn: a person over the same box.
    pet_be = _PetBackend([_animal()], [], turn_humans=[_human(w=100, h=100, score=0.85)])
    # A face elsewhere keeps the orientation probe out of this test — the photo
    # is already upright, only the animal box is in question.
    face_be = _FaceBackend([_face()])

    stats = _run(conn, Config(), pet_be, face_be, monkeypatch)

    assert stats.human_animals_dropped == 1
    assert stats.animals == 0
    assert stats.faces_found == 1  # no animal left to suppress it
    assert pet_be.turn_passes == 2  # both turns, and only because a box
    conn.close()  # survived the upright check


def test_a_sideways_photo_is_turned_upright_and_redetected(tmp_path, monkeypatch):
    """The archive's rotated re-exports: the photo reads as a `dog` filling the
    frame as stored, and as a confident person once turned. Record the turn and
    redo detection there, so the boxes belong to the frame the app shows."""
    conn = _catalog(tmp_path)
    pet_be = _PetBackend([_animal(w=100, h=110)], [])
    pet_be.turn_humans = [_human(w=110, h=100, score=0.90)]
    face_be = _FaceBackend([], turned_faces=[_face()])

    stats = _run(conn, Config(), pet_be, face_be, monkeypatch)

    assert stats.rotated == 1
    assert stats.faces_found == 1  # the face only the turn could see
    row = conn.execute("SELECT * FROM orientation").fetchone()
    assert row["rotate_deg"] in (90, 270)
    assert row["source"] == "person"
    assert row["confidence"] == pytest.approx(0.90)
    assert face_be.reports == 2  # upright, then the real frame
    conn.close()


def test_several_faces_agreeing_on_a_turn_turn_the_photo(tmp_path, monkeypatch):
    """A sideways group photo: nothing resolves as stored, but a quarter turn
    yields a quorum of faces. Several people are never all lying down the same
    way, so this needs none of the person path's guards — the subject need not
    fill the frame, and the bogus pet sitting on top of them goes with it."""
    conn = _catalog(tmp_path)
    pet_be = _PetBackend([_animal(species="bird", w=40, h=30, score=0.83)], [])
    face_be = _FaceBackend(
        [], turned_faces=[_face(), _face(x=60)], probe={90: (0.78, 0.71, 0.70, 0.68, 0.61)}
    )

    stats = _run(conn, Config(), pet_be, face_be, monkeypatch)

    assert stats.rotated == 1
    assert stats.faces_found == 2  # people the upright pass never saw
    assert stats.animals == 0  # and no phantom pet over them
    row = conn.execute("SELECT * FROM orientation").fetchone()
    assert (row["rotate_deg"], row["source"]) == (90, "faces")
    assert row["confidence"] == pytest.approx(0.78)
    conn.close()


def test_two_weak_faces_are_not_a_quorum(tmp_path, monkeypatch):
    """An upright meme collage yields two weak faces at a turn. Two was not
    enough to trust on this archive."""
    conn = _catalog(tmp_path)
    pet_be = _PetBackend([], [_human(w=30, h=40, score=0.4)])
    face_be = _FaceBackend([], probe={270: (0.67, 0.61)})

    stats = _run(conn, Config(), pet_be, face_be, monkeypatch)

    assert stats.rotated == 0
    assert conn.execute("SELECT COUNT(*) FROM orientation").fetchone()[0] == 0
    conn.close()


def test_one_rotated_face_is_never_a_reason_to_turn(tmp_path, monkeypatch):
    """A lone face that only resolves when turned is a doll, a cake figurine or
    someone lying down far more often than a sideways photo."""
    conn = _catalog(tmp_path)
    pet_be = _PetBackend([], [_human(w=30, h=40, score=0.4)])
    face_be = _FaceBackend([], probe={90: (0.95,)})

    stats = _run(conn, Config(), pet_be, face_be, monkeypatch)

    assert stats.rotated == 0
    conn.close()


def test_someone_lying_down_does_not_turn_a_correct_photo(tmp_path, monkeypatch):
    """Kids lying prone on grass: a turn reads them as upright people, but they
    are a detail of a landscape, not the photo. Leave it alone — and don't pay
    for the person probe at all, since a small subject can never win it."""
    conn = _catalog(tmp_path)
    pet_be = _PetBackend([_animal(w=12, h=8)], [])
    pet_be.turn_humans = [_human(w=12, h=8, score=0.85)]  # small in frame
    face_be = _FaceBackend([])

    stats = _run(conn, Config(), pet_be, face_be, monkeypatch)

    assert stats.rotated == 0
    assert conn.execute("SELECT COUNT(*) FROM orientation").fetchone()[0] == 0
    # Only the pet cross-check's two turns; the orientation probe would have
    # added three more YOLOX calls, and it was skipped before spending them.
    assert pet_be.human_calls == 2
    conn.close()


def test_a_confident_pet_outweighs_a_mediocre_person_reading(tmp_path, monkeypatch):
    """A cat close-up reads as a weak `person` from some angle. The photo is a
    more confident cat the way it is stored, so nothing moves — and the cat is
    still a pet."""
    conn = _catalog(tmp_path)
    pet_be = _PetBackend([_animal(species="cat", w=100, h=110, score=0.92)], [])
    pet_be.turn_humans = [_human(w=110, h=100, score=0.80)]
    face_be = _FaceBackend([])

    stats = _run(conn, Config(), pet_be, face_be, monkeypatch)

    assert stats.rotated == 0
    assert stats.animals == 1
    assert conn.execute("SELECT COUNT(*) FROM orientation").fetchone()[0] == 0
    conn.close()


def test_a_bird_that_reads_as_a_person_when_turned_stays_a_bird(tmp_path, monkeypatch):
    """A bird in flight reads as a person from some angle. Evidence found only
    by turning the image has to at least match the animal it would overturn."""
    conn = _catalog(tmp_path)
    pet_be = _PetBackend(
        [_animal(species="bird", score=0.93)], [], turn_humans=[_human(w=100, h=100, score=0.55)]
    )
    face_be = _FaceBackend([_face()])

    stats = _run(conn, Config(), pet_be, face_be, monkeypatch)

    assert stats.animals == 1
    assert stats.human_animals_dropped == 0
    conn.close()


def test_a_portrait_of_someone_lying_down_is_left_alone(tmp_path, monkeypatch):
    """A close-up of someone lying in bed fills the frame and reads as an
    upright person once turned — identical to a sideways photo by every other
    measure. It is already portrait, and a frame-filling person is a portrait
    composition, so there is nothing to fix."""
    conn = _catalog(tmp_path)
    pet_be = _PetBackend([_animal(w=90, h=110)], [], turn_humans=[_human(w=110, h=90, score=0.95)])

    stats = _run(
        conn, Config(), pet_be, _FaceBackend([]), monkeypatch, shape=(_IMG_W, _IMG_H)
    )  # stored portrait

    assert stats.rotated == 0
    conn.close()


def test_a_weak_person_reading_never_turns_a_photo(tmp_path, monkeypatch):
    """A wrongly turned photo is worse than an untouched one, so a turn that is
    only slightly more person-shaped is not enough."""
    conn = _catalog(tmp_path)
    pet_be = _PetBackend(
        [_animal(w=100, h=110)], [], turn_humans=[_human(w=110, h=100, score=0.55)]
    )
    face_be = _FaceBackend([])

    stats = _run(conn, Config(), pet_be, face_be, monkeypatch)

    assert stats.rotated == 0
    assert conn.execute("SELECT COUNT(*) FROM orientation").fetchone()[0] == 0
    conn.close()


def test_an_upright_photo_is_never_probed(tmp_path, monkeypatch):
    """The common case must not pay for the orientation check."""
    conn = _catalog(tmp_path)
    pet_be = _PetBackend([], [_human()])
    face_be = _FaceBackend([_face()])

    _run(conn, Config(), pet_be, face_be, monkeypatch)

    assert pet_be.human_calls == 0
    assert face_be.reports == 1
    conn.close()


def test_a_photo_with_nothing_in_it_is_never_probed(tmp_path, monkeypatch):
    """No face, but also no person or animal: there is nothing to go on, so no
    probe — most of the archive's landscapes land here."""
    conn = _catalog(tmp_path)
    pet_be = _PetBackend([], [])

    _run(conn, Config(), pet_be, _FaceBackend([]), monkeypatch)

    assert pet_be.human_calls == 0
    conn.close()


@pytest.mark.parametrize("deg,expected", [(90, (2, 3)), (180, (3, 2)), (270, (2, 3))])
def test_rotate_image_turns_clockwise(deg, expected):
    img = np.arange(6, dtype="uint8").reshape(3, 2)  # 3 rows x 2 cols
    turned = dx.rotate_image(img, deg)
    assert turned.shape == expected
    # top-left of the original must land where a clockwise turn puts it
    corner = {90: turned[0, -1], 180: turned[-1, -1], 270: turned[-1, 0]}[deg]
    assert corner == img[0, 0]


@pytest.mark.parametrize("k", [1, 3])
def test_boxes_from_a_quarter_turn_map_back_to_the_upright_frame(k):
    """Round-trip against a real rotation: mark a patch in the upright frame,
    rotate, take the patch's box there, and it must map back onto itself."""
    w, h = 400, 300
    x, y, bw, bh = 310, 40, 50, 80
    img = np.zeros((h, w), "uint8")
    img[y : y + bh, x : x + bw] = 255

    rows, cols = np.nonzero(np.rot90(img, k))
    rotated = _human(
        x=int(cols.min()),
        y=int(rows.min()),
        w=int(cols.max() - cols.min() + 1),
        h=int(rows.max() - rows.min() + 1),
    )
    (back,) = dx._rotate_boxes_back([rotated], k, w, h)

    assert (back.x, back.y, back.w, back.h) == (x, y, bw, bh)
