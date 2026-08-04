"""What a detect pass produces: one frame's findings, one file's, one run's.

These three shapes are the only thing every other module in ``detect/`` has in
common -- the frame decoder fills a ``Found``, the video sampler folds several
of those into a ``FileResult``, ``persist.py`` writes one, and ``extract.py``
totals them into ``DetectStats``. Kept in their own module with no logic and no
sibling imports so that chain can be a straight line: without it, ``video.py``
would need a shape defined in ``extract.py`` while ``extract.py`` calls into
``video.py``, which is a cycle.

The two detector names live here for the same reason: ``persist.py`` decides
what to write from them and ``extract.py`` decides what to run, and neither may
import the other's constants without closing that same loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..faces import backend as face_backend

# The two detectors a pass can be asked for. An archive may have People without
# Pets or the other way round (organize_archive/features.py), and this set is
# what says which. It is the *asked for* set, never the *loaded* set: a wanted
# detector whose weights fail to load still marks the files it walked, or the
# stage would find them pending again and relaunch itself for ever.
FACE = "face"
PET = "pet"
BOTH_DETECTORS = frozenset({FACE, PET})


@dataclass
class DetectStats:
    """One run's totals, as reported by ``extract()`` and shown by the CLI/GUI."""

    processed: int = 0  # files examined this run (images + videos)
    videos: int = 0  # of which were videos
    video_frames: int = 0  # sampled keyframes actually decoded
    faces_found: int = 0
    images_with_faces: int = 0
    animals: int = 0
    photos_with_animals: int = 0
    nonhuman_suppressed: int = 0  # faces dropped as an animal's own face
    human_animals_dropped: int = 0  # "pets" that a person box exposed as people
    rotated: int = 0  # photos found to be stored sideways
    candidates: int = 0
    rejected_score: int = 0
    rejected_size: int = 0
    rejected_clipped: int = 0
    rejected_nonhuman: int = 0
    errors: int = 0
    error_samples: list = field(default_factory=list)


@dataclass
class Found:
    """Everything one decoded frame yielded, already cross-checked."""

    faces: list = field(default_factory=list)  # kept, human
    animals: list = field(default_factory=list)  # kept, real animals
    humans: list = field(default_factory=list)  # person boxes (context)
    # The raw detector output. Defaulted the same way as FileResult.report
    # rather than to None: decode_frame() has always replaced this with a real
    # report or left the empty one in place, so None was a state no reader ever
    # had to handle and every reader was written assuming it could not happen.
    report: face_backend.DetectionReport = field(default_factory=face_backend.DetectionReport)
    human_animals: int = 0  # pets that were really people
    # (face, index into `animals`) for each face the veto dropped. Kept, not
    # just counted: they are written to nonhuman_detections so the veto stays
    # reviewable -- see detect/persist.py::_save_suppressed.
    suppressed: list = field(default_factory=list)
    max_subject_share: float = 0.0  # biggest box, as a frame share
    animal_score: float = 0.0  # best animal reading, pre-veto


@dataclass
class FileResult:
    """One decoded file's detections, shaped for persistence."""

    # [(Face|AnimalDetection, frame_offset|None), ...]; offset is
    # always None for a photo, and for a video after collapsing.
    face_hits: list = field(default_factory=list)
    animal_hits: list = field(default_factory=list)
    report: face_backend.DetectionReport = field(default_factory=face_backend.DetectionReport)
    human_animals: int = 0
    suppressed_hits: list = field(default_factory=list)  # (face, animal index)
    rotate: int = 0
    confidence: float = 0.0
    orient_source: str = ""
