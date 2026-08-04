"""Detecting in a video: where to sample, and collapsing what repeats.

A video is not one frame, so it needs two things a photo does not -- a rule for
which moments to look at, and a rule for recognising that the person in frame 2
is the person in frame 4. Both live here, together with the loop that applies
them, because they only make sense as a set.

There is no orientation resolution in this file, and that is deliberate: ffmpeg
applies the container's rotation metadata when it extracts a frame, so every
sampled frame arrives upright by construction and a video never gets an
``orientation`` row.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from .. import thumbnails
from ..config import Config
from ..faces.backend import Face, FaceBackend
from ..pets.backend import PetBackend
from .frame import detect_on, load_bgr
from .results import DetectStats, FileResult, Found

# -- video sampling ----------------------------------------------------------
# Frames sampled per video and where in the clip: evenly spread but pulled in
# from the very ends, where a title card or a black opening/closing frame sits
# more often than a moment that actually represents the video. Same idea as
# services/semantic.py's _VIDEO_FRAME_FRACTIONS (0.15, 0.5, 0.85 for 3 frames),
# generalized to any frame count with the same 0.15 margin on each end.
_VIDEO_FRAME_MARGIN = 0.15


def _video_frame_fractions(n: int, margin: float = _VIDEO_FRAME_MARGIN) -> tuple[float, ...]:
    if n <= 0:
        return ()
    if n == 1:
        return (0.5,)
    step = (1 - 2 * margin) / (n - 1)
    return tuple(margin + i * step for i in range(n))


def _format_offset(secs: float) -> str:
    secs = max(0.0, secs)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def video_offsets(duration_s: float | None, n: int) -> list[str]:
    """ffmpeg ``-ss`` offsets for up to ``n`` frames spread across a video.

    Mirrors ``services.semantic._video_frame_offsets``: falls back to one fixed
    early offset when the duration is unknown (enrich hasn't run yet, or
    ffprobe couldn't read this container) rather than refusing to detect the
    video at all. Never returns a duplicate offset -- a very short clip whose
    fractions round to the same timestamp yields fewer, not repeated, frames.
    """
    if n <= 0:
        return []
    # duration_s comes straight from a media_meta column: None when enrich has
    # not run yet, and not guaranteed numeric even when it has. Coercing inside
    # the try IS the check -- the ignore is for the None the except catches.
    try:
        duration = float(duration_s)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        return ["00:00:01"]
    offsets: list[str] = []
    seen: set[str] = set()
    for frac in _video_frame_fractions(n):
        text = _format_offset(min(duration * frac, max(0.0, duration - 0.05)))
        if text not in seen:
            seen.add(text)
            offsets.append(text)
    return offsets


# -- per-video duplicate collapse ---------------------------------------------
# The same person or animal can be detected in several of a video's sampled
# frames. Left alone that would flood clustering with near-duplicate rows and
# inflate every count (faces_found, animals, "N people in this video" ...), so
# each surviving detection must be the ONE row for a given individual, kept
# from whichever frame gave the best-quality read of them.


def _best_face_quality(face: Face) -> float:
    # quality_score is the aligned-crop focus/exposure measure (0-1); det_score
    # (`.score`) is the fallback when it isn't populated (defensive -- the real
    # Face dataclass always sets it, but keeps this helper usable on stand-ins).
    q = getattr(face, "quality_score", None)
    # Annotated rather than returned directly: getattr is Any, and
    # warn_return_any would let that out through a signature promising a float.
    best: float = q if q else face.score
    return best


def collapse_video_faces(entries: list[tuple], threshold: float) -> list[tuple]:
    """Collapse near-duplicate faces across a video's sampled frames.

    ``entries`` is ``[(Face, frame_offset), ...]`` in frame order. Each face is
    compared against the ones already kept (not pairwise against every other
    detection) by cosine similarity of their L2-normalized ArcFace embeddings
    -- cheap and order-independent enough for the handful of frames a video
    contributes; a video with hundreds of faces would want something smarter,
    but ``detect_video_frames`` is small by design. A similarity at/above
    ``threshold`` is the same person across frames; the one with the higher
    quality_score (falling back to det_score) is kept, so the surviving row's
    frame_offset is a frame the app can still re-derive a good crop from.
    """
    import numpy as np

    kept: list[list] = []  # [Face, offset], mutated in place as better hits arrive
    for face, offset in entries:
        best_i, best_sim = -1, -1.0
        for i, (kface, _koff) in enumerate(kept):
            sim = float(np.dot(face.embedding, kface.embedding))
            if sim > best_sim:
                best_sim, best_i = sim, i
        if best_i >= 0 and best_sim >= threshold:
            kface, _koff = kept[best_i]
            if _best_face_quality(face) > _best_face_quality(kface):
                kept[best_i] = [face, offset]
        else:
            kept.append([face, offset])
    return [(f, o) for f, o in kept]


def collapse_video_animals(entries: list[tuple], threshold: float) -> list[tuple]:
    """Same idea as ``collapse_video_faces``, for animal detections.

    Two boxes only ever collapse when they are additionally the same
    ``species`` -- a cat and a dog reading similarly on the DINOv2 embedding
    (rare, but possible for a low-detail crop) must never merge. The better of
    a pair is the higher ``det_score`` (animals have no separate quality
    metric).
    """
    import numpy as np

    kept: list[list] = []
    for animal, offset in entries:
        best_i, best_sim = -1, -1.0
        for i, (kanimal, _koff) in enumerate(kept):
            if kanimal.species != animal.species:
                continue
            sim = float(np.dot(animal.embedding, kanimal.embedding))
            if sim > best_sim:
                best_sim, best_i = sim, i
        if best_i >= 0 and best_sim >= threshold:
            kanimal, _koff = kept[best_i]
            if animal.score > kanimal.score:
                kept[best_i] = [animal, offset]
        else:
            kept.append([animal, offset])
    return [(a, o) for a, o in kept]


def _accumulate_frame(
    found: Found,
    result: FileResult,
    stats: DetectStats,
    offset: str,
    raw_faces: list[tuple],
    raw_animals: list[tuple],
) -> None:
    """Fold one sampled frame's findings into the running per-file totals."""
    result.report.candidates += found.report.candidates
    for reason, n in found.report.rejected.items():
        result.report.rejected[reason] = result.report.rejected.get(reason, 0) + n
    stats.human_animals_dropped += found.human_animals
    stats.nonhuman_suppressed += len(found.suppressed)
    raw_faces.extend((fc, offset) for fc in found.faces)
    raw_animals.extend((a, offset) for a in found.animals)


def detect_video(
    path: Path,
    cfg: Config,
    cache_dir: str,
    row: sqlite3.Row,
    face_be: FaceBackend | None,
    pet_be: PetBackend | None,
    stats: DetectStats,
    commit_if_due: Callable[..., None],
) -> FileResult:
    """Sample a video's frames, detect on each, and collapse repeats across them.

    ``stats`` is updated per frame directly (not returned and added by the
    caller), so a video that fails partway through keeps the frames it did
    finish -- matching what the inline loop this replaces did.
    """
    fid = row["id"]
    result = FileResult()
    # (detection, offset) pairs, the shape collapse_video_* takes.
    raw_faces: list[tuple] = []
    raw_animals: list[tuple] = []
    for offset in video_offsets(row["duration_s"], cfg.detect_video_frames):
        frame_path = thumbnails.detect_frame_for(
            cache_dir, fid, path, offset, cfg.detect_video_frame_px, sha256=row["sha256"]
        )
        if frame_path is None:
            continue  # this offset alone failed; try the rest
        stats.video_frames += 1
        img, scale = load_bgr(str(frame_path), cfg.detect_max_side)
        found = detect_on(img, scale, cfg, face_be, pet_be)
        _accumulate_frame(found, result, stats, offset, raw_faces, raw_animals)
        # A video costs one decode+detect PER sampled frame, so the same
        # time-budget relief extract() applies between files is also applied
        # between frames here -- otherwise a single multi-frame video could
        # itself hold the writer past other connections' busy_timeout.
        commit_if_due()
    # Same person/animal across several frames must not become several rows.
    result.face_hits = collapse_video_faces(raw_faces, cfg.detect_video_same_face)
    result.animal_hits = collapse_video_animals(raw_animals, cfg.detect_video_same_animal)
    # No frame at all (no ffmpeg, unreadable container) is a clean permanent
    # skip, not an error: the caller's scan markers are still written with zero
    # counts, so this video is never retried forever.
    return result
