"""The enrich stage: resolve dates, GPS and Takeout metadata for scanned files."""

from __future__ import annotations

from ..job import JobContext, Runner


def run(ctx: JobContext) -> None:
    from ...metadata import enrich as enrich_mod

    conn, job = ctx.require_conn(), ctx.job
    root_ids = (job.root_id,) if job.root_id else None
    # Cumulative over every file that could carry a date, not over this run's
    # backlog: the backlog is what is *left*, so measuring against it restarted
    # the bar at 0% of a total that had quietly shrunk to match, and a run
    # resumed after a pause looked like it had thrown away what it did. Same
    # shape the detect and semantic stages already use -- total is the whole
    # population, done starts at how much of it is already resolved.
    #
    # `fixed_total` is what stops enrich() from setting its own total, which is
    # the pending count and would undo this on the first call.
    total = enrich_mod.count_dateable(conn, root_ids)
    already = max(0, total - enrich_mod.pending_count(conn, root_ids))
    job.total, job.done = total, already
    prog = ctx.progress(base=already, fixed_total=True)
    stats = enrich_mod.enrich(conn, ctx.cfg, progress=prog, root_ids=root_ids)
    job.message = (
        f"{stats.processed} processed, "
        f"{stats.with_takeout} Takeout-matched, "
        f"{stats.with_gps} with GPS"
    )


RUNNER = Runner(kind="enrich", run=run, takes_write_lock=False)
