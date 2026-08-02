"""Fused detection: find people (SCRFD) and animals (YOLOX) in ONE image pass.

The old pipeline detected pets and faces in two separate stages, each decoding
every photo independently — so ~150k images were decoded twice. Here each image
is decoded a single time (at ``cfg.detect_max_side``) and both detectors run on
that one array.

The cross-check between them is anchored on one signal: the COCO ``person`` box
that the same YOLOX pass already produces (see pets/backend.py). It arbitrates
in both directions, because both detectors fail on humans who are not vertical
in the frame — a photo stored sideways, or someone lying down:

* an animal box that *is* a person box (high IoU) is a misread human, not a pet.
  YOLOX calls a non-vertical person ``dog`` with real confidence; rotating the
  same photo upright flips it back to ``person``, so when nothing vetoes an
  animal box upright the box is re-tested on the quarter-turns before it is
  believed (only for the few images that have animal boxes at all).
* a face inside a person box is human and is never suppressed; a face inside an
  animal box with no person over it is an animal's face and is dropped from
  People -- but *recorded*, in ``nonhuman_detections``, so the drop stays
  reviewable. The detector is guessing here, and a guess a user cannot overrule
  is the wrong kind of confident (see detect/persist.py).

That replaces the old one-directional rule, which dropped any face merely
*contained* in an animal box — so a bogus full-frame "dog" over a reclining
person deleted the real face from People *and* kept the phantom pet.

The same signal gives the stage its other job: **resolving true orientation**.
A quorum of faces — or a ``person`` box — that resolves at a quarter turn and
not upright says which way up the photo really is; it is recorded in
``orientation`` and detection is redone there, so the boxes and the app's view
of the photo are both upright. Deliberately narrow — see ``orientation.py``,
which holds that rule and the counterexamples each of its guards answers.

Resumable and incremental, like the stages it replaces: an image is pending when
it lacks a current ``face_scan`` OR a current ``pet_scan`` row, and is processed
as a unit: both detectors run, and every detection row the file has is rewritten
together (``detect/persist.py``), so the cross-check is always consistent.
Read-only over originals. Clustering into people/pets is a separate step
(faces/pets cluster.py).

This module is the *driver*: which files are pending, loading the backends,
batching, committing, and totalling. The detection itself is split across
siblings, all of which are readable without this one -- ``frame.py`` (decode a
frame and cross-check the two detectors on it), ``geometry.py`` (box maths and
quarter-turns), ``orientation.py`` (which way up), ``video.py`` (where to sample
a clip and how to collapse repeats), ``results.py`` (the three result shapes)
and ``persist.py`` (writing a file's rows).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Config
from ..db import database as db
from ..faces import backend as face_backend
from ..faces import fiqa
from ..pets import backend as pet_backend
from ..pets.extract import scan_source as pet_scan_source
from .frame import detect_on, load_bgr
from .geometry import rotate_image
from .orientation import resolve_rotation
from .persist import rewrite_file_detections, write_scan_markers
from .results import DetectStats, FileResult
from .video import detect_video

logger = logging.getLogger(__name__)


# hidden=0 skips non-canonical duplicate copies (dedup runs before detection);
# re-detecting a duplicate is pure waste — the canonical copy is scanned instead.
#
# Videos only join the pending population when detect_video_frames > 0 --
# otherwise a video would sit "pending" forever with the feature turned off,
# and the stage would never report itself as up to date.
def _media_types(cfg: Config) -> str:
    return "('image','video')" if cfg.detect_video_frames > 0 else "('image')"


def _pending_where(cfg: Config) -> str:
    return f"""
        f.present=1 AND f.media_type IN {_media_types(cfg)} AND f.hidden=0
        AND (fs.file_id IS NULL
             OR ps.file_id IS NULL
             OR ps.source_sha256 IS NOT f.sha256
             OR ps.model_source IS NOT :pet_src)
    """


def image_count(conn, cfg: Config | None = None, root_id: int | None = None) -> int:
    """Total present, canonical (non-duplicate) media the stage counts.

    ``cfg`` is optional so old call sites keep working, but pass it: without
    it this always counts images only, which under-counts the population
    relative to ``pending_count`` while video detection is enabled and makes
    "done / total" progress look wrong.
    """
    media_types = _media_types(cfg) if cfg is not None else "('image')"
    rc = " AND f.root_id=?" if root_id is not None else ""
    params = (root_id,) if root_id is not None else ()
    return conn.execute(
        f"""SELECT COUNT(*) FROM files f
            WHERE f.present=1 AND f.media_type IN {media_types} AND f.hidden=0{rc}""",
        params,
    ).fetchone()[0]


def pending_count(conn, cfg: Config, root_id: int | None = None) -> int:
    """Present canonical media missing a current face OR pet scan."""
    rc = " AND f.root_id=:root" if root_id is not None else ""
    p: dict[str, Any] = {"pet_src": pet_scan_source(cfg)}
    if root_id is not None:
        p["root"] = root_id
    return conn.execute(
        f"""SELECT COUNT(*) FROM files f
            LEFT JOIN face_scan fs ON fs.file_id=f.id
            LEFT JOIN pet_scan ps ON ps.file_id=f.id
            WHERE {_pending_where(cfg)}{rc}""",
        p,
    ).fetchone()[0]


def _pending(conn, cfg: Config, batch_size: int):
    return conn.execute(
        f"""SELECT f.id, f.rel_path, f.sha256, f.media_type, r.path AS root_path,
                   mm.duration_s, (fs.file_id IS NULL) AS need_face
            FROM files f JOIN roots r ON r.id=f.root_id
            LEFT JOIN face_scan fs ON fs.file_id=f.id
            LEFT JOIN pet_scan ps ON ps.file_id=f.id
            LEFT JOIN media_meta mm ON mm.file_id=f.id
            WHERE {_pending_where(cfg)}
            ORDER BY f.id
            LIMIT :lim""",
        {"pet_src": pet_scan_source(cfg), "lim": batch_size},
    ).fetchall()


def available() -> bool:
    """True if at least one detector can run (faces and/or pets)."""
    return face_backend.available() or pet_backend.available()


def make_backends(cfg: Config, log=None):
    """Build whichever detectors are available (loads the ONNX models once).

    Returns ``(face_be, pet_be)``; either may be None when its optional deps or
    models are missing, so the stage degrades gracefully (faces-only or pets-only)
    instead of failing the whole pass."""
    face_be = pet_be = None
    if face_backend.available():
        try:
            face_be = face_backend.FaceBackend(
                cfg.cache_dir,
                min_score=cfg.faces_min_score,
                min_px=cfg.faces_min_px,
                max_side=cfg.faces_max_side,
                det_size=cfg.faces_det_size,
                max_clipped_fraction=cfg.faces_max_clipped_fraction,
                min_focus=cfg.faces_min_focus,
                max_extreme_fraction=cfg.faces_max_extreme_fraction,
                quality_version=cfg.faces_quality_version,
                log=log,
            )
        except Exception as e:
            # Weights that could not be fetched or loaded must not take the other
            # detector down with them: report and carry on pets-only.
            if log:
                log(f"people detection unavailable: {e}")
            logger.warning("people detection unavailable", exc_info=True)
    if pet_backend.available():
        try:
            pet_be = pet_backend.PetBackend(
                cfg.cache_dir,
                min_score=cfg.pets_min_score,
                min_px=cfg.pets_min_px,
                max_side=cfg.pets_max_side,
                species=cfg.pets_species,
                human_min_score=cfg.pets_human_min_score,
                model_source=pet_scan_source(cfg),
                log=log,
            )
        except Exception as e:
            if log:
                log(f"pet detection unavailable: {e}")
            logger.warning("pet detection unavailable", exc_info=True)
    return face_be, pet_be


def _fix_sideways_photo(img, scale, found, cfg, face_be, pet_be):
    """Is this photo stored sideways?

    Someone/something is here but no face resolved: that is the signature of
    a photo whose pixels are turned. Probing costs three score-only SCRFD
    passes and is skipped for the vast majority of images, which answer on
    the first pass. SCRFD finding nothing at all is the signal, not the
    cross-check's verdict: a face it did find and an animal then claimed
    says the photo was readable, so leave it be.
    """
    rotate, conf, orient_src = 0, 0.0, ""
    if face_be is not None and not found.report.faces and (found.animals or found.humans):
        rotate, conf, orient_src = resolve_rotation(
            img, face_be, pet_be, cfg, found.max_subject_share, found.animal_score
        )
        if rotate:
            img = rotate_image(img, rotate)
            found = detect_on(img, scale, cfg, face_be, pet_be)
    return found, rotate, conf, orient_src


def _detect_photo(path, cfg, face_be, pet_be) -> FileResult:
    """Decode one image and detect on it, self-correcting orientation if needed."""
    img, scale = load_bgr(str(path), cfg.detect_max_side)
    found = detect_on(img, scale, cfg, face_be, pet_be)
    found, rotate, conf, orient_src = _fix_sideways_photo(img, scale, found, cfg, face_be, pet_be)
    return FileResult(
        face_hits=[(fc, None) for fc in found.faces],
        animal_hits=[(a, None) for a in found.animals],
        report=found.report,
        human_animals=found.human_animals,
        suppressed_hits=found.suppressed,
        rotate=rotate,
        confidence=conf,
        orient_source=orient_src,
    )


def _prepare_backends(conn, cfg: Config, progress, limit, face_be, pet_be):
    """Load whichever detectors weren't already provided, and size the run."""
    if not available():
        raise RuntimeError("detect backend unavailable (needs faces and/or pets)")
    if face_be is None and pet_be is None:
        face_be, pet_be = make_backends(
            cfg, log=(lambda m: progress.update(0, 0, m)) if progress else None
        )
        if face_be is None and pet_be is None:
            # Both sets of weights failed to load. Stop here rather than walk the
            # pending images and mark them scanned with nothing detected.
            raise RuntimeError(
                "detect backend unavailable: neither the face nor the pet models "
                "could be loaded (see the messages above)"
            )
    # The FIQA assessor is attached per run rather than baked into the backend:
    # it depends on the calibration row, which this very run may create (see
    # _maybe_bootstrap_fiqa), and the backend is often reused across chunked runs.
    if face_be is not None:
        face_be.assessor = fiqa.make_assessor(conn, cfg)

    total = pending_count(conn, cfg)
    if limit is not None:
        total = min(total, limit)
    if progress is not None:
        progress.total = total
    return face_be, pet_be


def _make_committer(conn):
    """Build a callable that commits ``conn`` at most once every 2 seconds.

    ``force=True`` commits unconditionally and resets the clock either way --
    used once per batch, where a commit is always wanted.
    """
    import time as _time

    last = _time.monotonic()

    def commit_if_due(force: bool = False):
        nonlocal last
        if force or (_time.monotonic() - last) >= 2:
            conn.commit()
            last = _time.monotonic()

    return commit_if_due


def _finish_row(stats: DetectStats, progress, result: FileResult, path):
    """Roll one file's outcome into the run's stats and progress reporting."""
    n_faces = len(result.face_hits)
    n_animals = len(result.animal_hits)
    stats.candidates += result.report.candidates
    stats.rejected_score += result.report.rejected.get("score", 0)
    stats.rejected_size += result.report.rejected.get("size", 0)
    stats.rejected_clipped += result.report.rejected.get("clipped", 0)
    stats.rejected_nonhuman += result.report.rejected.get("nonhuman", 0)
    stats.processed += 1
    stats.faces_found += n_faces
    stats.animals += n_animals
    if n_faces:
        stats.images_with_faces += 1
    if n_animals:
        stats.photos_with_animals += 1
    if progress is not None:
        progress.update(stats.processed, 0, path.name)


def _maybe_bootstrap_fiqa(conn, cfg: Config, face_be, stats, progress):
    # Once enough feature norms exist, fix the FIQA calibration and tier the
    # backlog that was extracted before it. A no-op on every later batch
    # (one indexed lookup), so the archive is calibrated exactly once and a
    # face's tier never depends on which batch it happened to land in.
    if face_be is not None:
        before = fiqa.load_calibration(conn, cfg.faces_fiqa_model)
        if (
            before is None
            and fiqa.bootstrap_calibration(
                conn,
                cfg,
                log=(lambda m: progress.update(stats.processed, 0, m)) if progress else None,
            )
            is not None
        ):
            conn.commit()
            face_be.assessor = fiqa.make_assessor(conn, cfg)


@dataclass
class _Run:
    """Everything one ``extract()`` pass carries unchanged from file to file."""

    cfg: Config
    cache_dir: str
    face_be: object
    pet_be: object
    stats: DetectStats
    commit_if_due: object
    now: str
    pet_src: str


def _detect_one(run: _Run, row, path) -> FileResult:
    """Detect one pending file, photo or video, folding in its per-file stats."""
    if row["media_type"] == "video":
        run.stats.videos += 1
        return detect_video(
            path, run.cfg, run.cache_dir, row, run.face_be, run.pet_be, run.stats, run.commit_if_due
        )
    result = _detect_photo(path, run.cfg, run.face_be, run.pet_be)
    if result.rotate:
        run.stats.rotated += 1
    run.stats.human_animals_dropped += result.human_animals
    run.stats.nonhuman_suppressed += len(result.suppressed_hits)
    return result


def _process_file(conn, run: _Run, row, progress) -> None:
    """Detect one file, write its rows, and mark it scanned either way."""
    fid = row["id"]
    path = Path(row["root_path"]) / row["rel_path"]
    try:
        result = _detect_one(run, row, path)
        fiqa_model = getattr(getattr(run.face_be, "assessor", None), "model", None)
        rewrite_file_detections(conn, fid, run.now, result, run.pet_src, fiqa_model, row["sha256"])
    except Exception as e:  # bad/corrupt file, unreadable, etc.
        run.stats.errors += 1
        if len(run.stats.error_samples) < 5:
            run.stats.error_samples.append(f"{path.name}: {e}")
        # No exc_info: ~150k files means a traceback per bad file would
        # flood the log until rotation discards everything useful.
        logger.warning("detection failed for %s: %s", path.name, e)
        # Back to a blank result, because the scan markers below are written
        # whether or not this file succeeded: reporting the detected counts for
        # a file whose rows only partly landed would claim faces the catalog
        # does not have. See write_scan_markers.
        result = FileResult()
    write_scan_markers(conn, fid, result, row["sha256"], run.pet_src, run.now)
    _finish_row(run.stats, progress, result, path)


def extract(
    conn,
    cfg: Config,
    *,
    progress=None,
    batch_size: int = 32,
    limit: int | None = None,
    face_be=None,
    pet_be=None,
    cache_dir: str | None = None,
) -> DetectStats:
    """Detect people + animals for pending images/videos in one decode each.

    ``limit`` caps files this run (chunked runs / testing); None = until drained.
    Pass already-loaded backends to reuse them across chunks. Either backend may be
    None (feature unavailable): the pass then does only the other, and with no pet
    backend there is simply no animal cross-check.

    ``cache_dir`` is where sampled video keyframes are extracted to and cached
    (see ``thumbnails.detect_frame_for``); it defaults to ``cfg.cache_dir`` for
    the CLI's single shared catalog, but the GUI's per-archive isolation means
    each archive has its OWN cache directory, so it must pass that archive's
    (``cfg.archive_cache_dir(root_id)``) explicitly rather than relying on the
    default.
    """
    stats = DetectStats()
    face_be, pet_be = _prepare_backends(conn, cfg, progress, limit, face_be, pet_be)
    run = _Run(
        cfg=cfg,
        cache_dir=cache_dir if cache_dir is not None else cfg.cache_dir,
        face_be=face_be,
        pet_be=pet_be,
        stats=stats,
        commit_if_due=_make_committer(conn),
        now=db.now_iso(),
        pet_src=pet_scan_source(cfg),
    )

    while True:
        remaining = None if limit is None else max(0, limit - stats.processed)
        if remaining == 0:
            break
        rows = _pending(conn, cfg, batch_size if remaining is None else min(batch_size, remaining))
        if not rows:
            break
        for row in rows:
            _process_file(conn, run, row, progress)
            # Flush to DB by time, not by batch size: a full 32-image batch is
            # a full SCRFD+YOLOX decode plus up to three extra YOLOX passes
            # each, which can hold the single writer well past the 30s
            # busy_timeout other connections (the parallel semantic worker,
            # HTTP handler writes) wait on -- surfacing as a spurious
            # "database is locked". This also cuts worst-case work lost on a
            # kill from a full batch to about 2 seconds.
            run.commit_if_due()
        run.commit_if_due(force=True)
        _maybe_bootstrap_fiqa(conn, cfg, face_be, stats, progress)

    return stats
