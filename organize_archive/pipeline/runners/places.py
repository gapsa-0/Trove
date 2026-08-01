"""The places stage: attach geotagged files to map places."""

from __future__ import annotations

from ..job import JobContext, Runner


def run(ctx: JobContext) -> None:
    # Keep map places in sync WITHOUT ever destroying user edits. Places are
    # durable entities: a root is clustered from scratch only the first time
    # (bootstrap); afterwards new geotagged files are attached incrementally
    # (assign_unplaced), so named/pinned places and manual attachments persist.
    # Each archive is now its own database, so this only ever touches the
    # one this job belongs to.
    from ...geo.clusters import assign_unplaced, cluster_places

    conn, job = ctx.conn, ctx.job
    job.total, job.done = 1, 0
    has_places = conn.execute(
        "SELECT 1 FROM place_clusters WHERE root_id=? LIMIT 1", (job.root_id,)
    ).fetchone()
    touched = assign_unplaced(conn, job.root_id).points if has_places else 0
    if not has_places:
        cluster_places(conn, job.root_id)
    job.done = 1
    job.message = f"{touched} new geotagged files placed"


RUNNER = Runner(kind="places", run=run)
