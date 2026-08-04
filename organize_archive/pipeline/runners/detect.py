"""The detect stage: people + pet detection, then re-cluster after each chunk."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from ..job import JobContext, Runner

# Images per detect-then-recluster chunk in the detect job (see run() below):
# small enough that people/pets appear early in a multi-hour run, large enough
# that repeated clustering stays a small fraction of total time. Detection is
# heavier per image than one detector was (SCRFD + YOLOX + embeds), so the
# chunk is smaller than the old faces chunk.
_DETECT_CHUNK = 600


@dataclass
class _Totals:
    """What one detect run found, summed across its chunks.

    A bundle rather than seven locals because all seven travel together from
    the chunk loop to the closing message, and passing them individually is
    how a helper ends up with an argument list nobody can read.
    """

    processed: int = 0
    faces: int = 0
    animals: int = 0
    suppressed: int = 0
    human_pets: int = 0
    turned: int = 0
    restored: int = 0


def _detect_backlog(
    ctx: JobContext, root_id: int, already: int, face_be: Any, pet_be: Any, want: frozenset[str]
) -> _Totals:
    """Detect in chunks, re-clustering people and pets after each one.

    Clustering inside the loop rather than once at the end is what makes both
    views fill in progressively during a run measured in hours. Only the half
    the archive asked for is re-clustered: rebuilding pets for an archive that
    switched Pets off would destroy and recreate every pet id (and with it the
    manual tags anchored to their names) to no purpose.

    The two backends are ``Any`` because their classes live in ``faces/`` and
    ``pets/``, which are still on mypy's not-reviewed list, so a real
    annotation would resolve to ``Any`` anyway -- and either may be None, since
    ``make_backends`` degrades to faces-only or pets-only.
    """
    from ...detect import extract as dx
    from ...detect.results import FACE, PET
    from ...faces import cluster as fc
    from ...pets import cluster as pc

    conn, job = ctx.require_conn(), ctx.job
    totals = _Totals()
    while True:
        ctx.raise_if_cancelled()
        prog = ctx.progress(base=already + totals.processed, fixed_total=True)
        st = dx.extract(
            conn,
            ctx.cfg,
            progress=prog,
            limit=_DETECT_CHUNK,
            face_be=face_be,
            pet_be=pet_be,
            cache_dir=ctx.cfg.archive_cache_dir(root_id),
            want=want,
        )
        if st.processed == 0:
            return totals
        totals.processed += st.processed
        totals.faces += st.faces_found
        totals.animals += st.animals
        totals.suppressed += st.nonhuman_suppressed
        totals.human_pets += st.human_animals_dropped
        totals.turned += st.rotated
        job.current = "grouping " + _subjects(want)
        if FACE in want:
            fc.cluster_faces(conn, ctx.cfg)
        if PET in want:
            pc.cluster_pets(conn, ctx.cfg, root_id=root_id)


def _subjects(want: frozenset[str]) -> str:
    """What this pass is looking for, for the one line the card shows."""
    from ...detect.results import FACE, PET

    names = [n for n, d in (("people", FACE), ("pets", PET)) if d in want]
    return " & ".join(names) + "…"


def _reattach_identities(ctx: JobContext, totals: _Totals, want: frozenset[str]) -> None:
    """Give names, pins and review answers back after an embedder migration.

    The backlog is empty by the time this runs, so a migration staged earlier
    now has every re-extracted face it needs. Only here, never mid-run: a
    partially re-extracted archive would strand identities on faces that have
    not been detected yet. The re-cluster afterwards is what makes the restored
    pins actually take effect.
    """
    from ...detect.results import FACE
    from ...faces import cluster as fc
    from ...faces import migrate_adaface

    conn = ctx.require_conn()
    if FACE not in want or not migrate_adaface.pending(conn):
        return
    ctx.job.current = "restoring names and corrections…"
    totals.restored = migrate_adaface.reattach(conn, ctx.cfg).faces_reattached
    fc.cluster_faces(conn, ctx.cfg)


def _summarise(conn: sqlite3.Connection, totals: _Totals, want: frozenset[str]) -> str:
    """The one line the finished stage card shows.

    Every clause after the first is conditional: a run that suppressed nothing
    and turned nothing says so by staying quiet, rather than reporting zeroes.
    A detector the archive did not ask for reports nothing at all -- the pet
    tables may still hold groups from before the feature was switched off, and
    quoting them as this run's findings would be a lie.
    """
    from ...detect.results import FACE, PET

    parts = []
    if FACE in want:
        people = conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
        parts.append(f"{totals.faces} faces · {people} people")
    if PET in want:
        groups = conn.execute("SELECT COUNT(*) FROM pets").fetchone()[0]
        parts.append(f"{totals.animals} animals · {groups} pet groups")
    return (
        " · ".join(parts)
        + (f" · {totals.suppressed} animal-face FPs dropped" if totals.suppressed else "")
        + (f" · {totals.human_pets} people misread as pets" if totals.human_pets else "")
        + (f" · {totals.turned} photos turned upright" if totals.turned else "")
        + (f" · {totals.restored} identities restored" if totals.restored else "")
    )


def run(ctx: JobContext) -> None:
    # Part of the automatic pipeline (after dedup). ONE decode per image runs
    # both detectors — people via SCRFD, animals via YOLOX — and the animal
    # boxes cross-check the faces inline (a face inside an animal box is
    # dropped from People). Detect in chunks and re-cluster people + pets after
    # each so both views fill in progressively during a long run.
    from ... import features
    from ...detect import extract as dx

    conn, job = ctx.require_conn(), ctx.job
    # detect is only ever started by the scheduler, always with the currently
    # open root's id -- see scan.py's comment for the same invariant.
    root_id = job.require_root()
    # People and Pets are chosen separately but share this one pass, so the
    # archive's feature set is what decides which detectors load, which files
    # count as pending, and whose rows this pass may rewrite.
    want = features.detectors(ctx.cfg.archive_features(root_id))

    # Everything up to the first chunk is setup, and on a first run it is the
    # longest wait in the app: ~310 MB of weights over whatever connection the
    # user has. The card shows that instead of a progress bar counting a
    # population nothing has started on yet (see JobContext.preparing).
    with ctx.preparing("counting photos to detect"):
        # Progress is cumulative over ALL canonical media, not just this run's
        # backlog: total = every canonical image (+ video, once video detection
        # is enabled), done starts at how many are already detected. So the
        # bar/% match the "Detected N / total" tile and survive resuming across
        # restarts (no misleading per-run total). cfg is passed so the
        # population matches pending_count's (both honour detect_video_frames).
        total = dx.image_count(conn, ctx.cfg, root_id)
        already = max(0, total - dx.pending_count(conn, ctx.cfg, root_id, want))
        job.total, job.done = total, already
        # Load the wanted detector model sets once and reuse across every chunk.
        # This is the stage's un-cancellable window: two ONNX sessions, seconds
        # of native code with nowhere to check the cancel event, so shutdown is
        # told not to wait on it (see JobContext.uninterruptible).
        with ctx.uninterruptible("loading detection models"):
            loaded = dx.make_backends(ctx.cfg, log=lambda m: setattr(job, "current", m), want=want)
        # Fail here rather than inside the first chunk: extract() would
        # otherwise load the models a second time to reach the same conclusion.
        loaded.require()
    totals = _detect_backlog(ctx, root_id, already, loaded.face, loaded.pet, want)
    _reattach_identities(ctx, totals, want)
    job.message = _summarise(conn, totals, want)


RUNNER = Runner(kind="detect", run=run)
