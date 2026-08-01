"""The pet-cluster stage: recompute pet groupings after a review edit."""

from __future__ import annotations

from ..job import JobContext, Runner


def run(ctx: JobContext) -> None:
    # Mirrors face_cluster.py; started after unmerge_pets so an undone
    # merge's fresh 'different' pet_links row takes effect immediately
    # rather than waiting for the next full detect chunk.
    from ...pets.cluster import cluster_pets

    conn, job = ctx.conn, ctx.job
    job.current = "reclustering pets after review…"
    stats = cluster_pets(conn, ctx.cfg, root_id=job.root_id)
    job.done = job.total = 1
    job.message = f"{stats.pets} pets · {stats.clustered} detections clustered"


RUNNER = Runner(kind="pet_cluster", run=run)
