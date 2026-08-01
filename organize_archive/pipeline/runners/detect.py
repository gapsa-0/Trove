"""The detect stage: people + pet detection, then re-cluster after each chunk."""

from __future__ import annotations

from ..job import JobContext, Runner

# Images per detect-then-recluster chunk in the detect job (see run() below):
# small enough that people/pets appear early in a multi-hour run, large enough
# that repeated clustering stays a small fraction of total time. Detection is
# heavier per image than one detector was (SCRFD + YOLOX + embeds), so the
# chunk is smaller than the old faces chunk.
_DETECT_CHUNK = 600


def run(ctx: JobContext) -> None:
    # Part of the automatic pipeline (after dedup). ONE decode per image runs
    # both detectors — people via SCRFD, animals via YOLOX — and the animal
    # boxes cross-check the faces inline (a face inside an animal box is
    # dropped from People). Detect in chunks and re-cluster people + pets after
    # each so both views fill in progressively during a long run.
    from ...detect import extract as dx
    from ...faces import cluster as fc
    from ...pets import cluster as pc

    conn, job = ctx.conn, ctx.job

    # Progress is cumulative over ALL canonical media, not just this run's
    # backlog: total = every canonical image (+ video, once video detection
    # is enabled), done starts at how many are already detected. So the
    # bar/% match the "Detected N / total" tile and survive resuming across
    # restarts (no misleading per-run total). cfg is passed so the
    # population matches pending_count's (both honour detect_video_frames).
    total = dx.image_count(conn, ctx.cfg, job.root_id)
    already = max(0, total - dx.pending_count(conn, ctx.cfg, job.root_id))
    job.total, job.done = total, already
    # Load both detector model sets once and reuse across every chunk.
    face_be, pet_be = dx.make_backends(ctx.cfg, log=lambda m: setattr(job, "current", m))
    processed = faces_found = animals = suppressed = human_pets = 0
    turned = 0
    while True:
        ctx.raise_if_cancelled()
        prog = ctx.progress(base=already + processed, fixed_total=True)
        st = dx.extract(
            conn,
            ctx.cfg,
            progress=prog,
            limit=_DETECT_CHUNK,
            face_be=face_be,
            pet_be=pet_be,
            cache_dir=ctx.cfg.archive_cache_dir(job.root_id),
        )
        if st.processed == 0:
            break
        processed += st.processed
        faces_found += st.faces_found
        animals += st.animals
        suppressed += st.nonhuman_suppressed
        human_pets += st.human_animals_dropped
        turned += st.rotated
        job.current = "grouping people & pets…"
        fc.cluster_faces(conn, ctx.cfg)
        pc.cluster_pets(conn, ctx.cfg, root_id=job.root_id)

    # The backlog is empty, so an embedder migration staged earlier now has
    # every re-extracted face it needs: give the names, pins and review
    # answers back to the faces they belong to, then cluster once more so
    # those restored pins actually take effect. Only here, never mid-run: a
    # partially re-extracted archive would strand identities on faces that
    # have not been detected yet.
    from ...faces import migrate_adaface

    restored = 0
    if migrate_adaface.pending(conn):
        job.current = "restoring names and corrections…"
        restored = migrate_adaface.reattach(conn, ctx.cfg).faces_reattached
        fc.cluster_faces(conn, ctx.cfg)

    people = conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
    groups = conn.execute("SELECT COUNT(*) FROM pets").fetchone()[0]
    job.message = (
        f"{faces_found} faces · {people} people · {animals} animals · "
        f"{groups} pet groups"
        + (f" · {suppressed} animal-face FPs dropped" if suppressed else "")
        + (f" · {human_pets} people misread as pets" if human_pets else "")
        + (f" · {turned} photos turned upright" if turned else "")
        + (f" · {restored} identities restored" if restored else "")
    )


RUNNER = Runner(kind="detect", run=run)
