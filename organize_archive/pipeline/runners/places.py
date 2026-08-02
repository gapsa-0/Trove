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

    conn, job = ctx.require_conn(), ctx.job
    # places is only ever started by the scheduler, always with the currently
    # open root's id -- see scan.py's comment for the same invariant.
    root_id = job.require_root()
    job.total, job.done = 1, 0
    has_places = conn.execute(
        "SELECT 1 FROM place_clusters WHERE root_id=? LIMIT 1", (root_id,)
    ).fetchone()
    touched = assign_unplaced(conn, root_id).points if has_places else 0
    if not has_places:
        cluster_places(conn, root_id)
    job.done = 1
    job.message = f"{touched} new geotagged files placed"


RUNNER = Runner(kind="places", run=run)
