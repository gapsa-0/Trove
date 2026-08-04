"""The face-cluster stage: recompute person groupings after a review edit."""

from __future__ import annotations

from ..job import JobContext, Runner


def run(ctx: JobContext) -> None:
    from ...faces.cluster import cluster_faces

    conn, job = ctx.require_conn(), ctx.job
    job.current = "reclustering people after review…"
    stats = cluster_faces(conn, ctx.cfg)
    job.done = job.total = 1
    job.message = f"{stats.people} people · {stats.clustered} faces clustered"


RUNNER = Runner(kind="face_cluster", run=run)
