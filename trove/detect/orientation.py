"""Deciding which way up a photo really is.

EXIF is applied on decode and settles most photos, but this archive is full of
re-exports whose pixels are turned while their orientation tag says they are
not, and on those every model in the stage fails at once -- SCRFD finds no face
and YOLOX calls the person a dog.

Two kinds of evidence, in order of how specific they are, one function each
below. Being wrong here is expensive: turning a correctly stored photo over is
worse than leaving a sideways one alone, so each guard in the weaker path
earns its place against a real counterexample from this archive. Both paths try
the quarter turns only -- pixels stored upside down are vanishingly rare, while
detectors fire happily on upside-down subjects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..config import Config
from ..faces.backend import FaceBackend
from ..pets.backend import PetBackend
from .geometry import rotate_image

if TYPE_CHECKING:
    import numpy as np


def _face_quorum_turn(img: np.ndarray, face_be: FaceBackend, cfg: Config) -> tuple[int, float, str]:
    """The turn at which several faces resolve, if there is one.

    Several faces that only resolve once the photo is turned is decisive --
    people do not all lie down in the same direction. This is what catches a
    sideways group photo, the case with the most to lose: one such photo in this
    archive shows five faces at 90 degrees and none at all as stored, so leaving
    it costs five people from People *and* leaves a phantom pet on top of them.

    A quorum is required because a *lone* rotated face is nearly always a doll,
    a cake figurine or someone lying down -- that was measured on this archive,
    and single-face evidence is not used at all.
    """
    best_deg = 0
    best: list[float] = []  # the accepted face scores at best_deg
    for deg in (90, 270):
        faces = [s for s in face_be.probe_faces(rotate_image(img, deg)) if s >= cfg.faces_min_score]
        if len(faces) > len(best):
            best_deg, best = deg, faces
    if len(best) >= cfg.orientation_min_faces:
        return best_deg, max(best), "faces"
    return 0, 0.0, ""


def _best_person(pet_be: PetBackend, img: np.ndarray) -> tuple[float, float]:
    """``(confidence, share of the frame)`` of the most confident person box."""
    h, w = img.shape[:2]
    frame = max(1, w * h)
    best = max(pet_be.detect_humans(img), key=lambda p: p.score, default=None)
    return (best.score, best.w * best.h / frame) if best else (0.0, 0.0)


def _person_box_turn(
    img: np.ndarray,
    pet_be: PetBackend | None,
    cfg: Config,
    subject_share: float,
    animal_score: float,
) -> tuple[int, float, str]:
    """The turn at which a dominant subject reads as a ``person`` and not upright.

    That asymmetry is the signature of a sideways-stored photo of a person, and
    it is what the pet cross-check already exploits. It carries less information
    than a face quorum, so it is fenced in much more tightly:

    * **The subject must fill the frame.** Someone lying on the grass in a
      landscape shot reads as an upright person once turned, exactly like a
      standing person in a sideways photo. The difference is that the sideways
      portrait's subject covers most of the frame (measured: 0.93) while the
      person lying down is a detail of a scene (0.03) whose own horizon settles
      the question.
    * **Only landscape becomes portrait.** A person filling the frame is a
      portrait composition, so the frame that needs turning is the one stored
      the wrong way round — landscape. Without this, a close-up of someone
      lying in bed (already portrait, subject filling it, and indistinguishable
      from a sideways photo by every other measure) gets turned on its side.
    * **It must beat the animal reading.** A cat close-up reads as a mediocre
      ``person`` from some angle; if the photo is a more confident animal the
      way it is stored, nothing moves.

    The three YOLOX passes are the most expensive thing in this stage and can
    only ever succeed on a dominant subject in a landscape frame, so a photo
    without one skips them entirely.
    """
    h, w = img.shape[:2]
    if pet_be is None or subject_share < cfg.orientation_min_subject or w <= h:
        return 0, 0.0, ""
    upright, _ = _best_person(pet_be, img)
    best_deg, best_score, best_share = 0, upright, 0.0
    for deg in (90, 270):
        score, share = _best_person(pet_be, rotate_image(img, deg))
        if score > best_score:
            best_deg, best_score, best_share = deg, score, share
    if (
        best_deg
        and best_score >= cfg.orientation_person_min
        and best_score - upright >= cfg.orientation_person_margin
        and best_share >= cfg.orientation_min_subject
        and best_score >= animal_score
    ):
        return best_deg, best_score, "person"
    return 0, 0.0, ""


def resolve_rotation(
    img: np.ndarray,
    face_be: FaceBackend | None,
    pet_be: PetBackend | None,
    cfg: Config,
    subject_share: float,
    animal_score: float,
) -> tuple[int, float, str]:
    """Find the quarter turn that makes this photo's subjects upright.

    Returns ``(degrees, confidence, source)``, or ``(0, 0.0, "")`` to leave the
    photo untouched. The face quorum is tried first: it is cheaper than the
    person path (SCRFD without ArcFace) and far more specific when it fires. It
    also needs none of the person path's guards, because it is already saying
    "several people are upright this way", which no lying-down or close-up pet
    case can imitate.
    """
    if face_be is not None:
        deg, conf, source = _face_quorum_turn(img, face_be, cfg)
        if deg:
            return deg, conf, source
    return _person_box_turn(img, pet_be, cfg, subject_share, animal_score)
