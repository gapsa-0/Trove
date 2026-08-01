"""The enrich stage: resolve dates, GPS and Takeout metadata for scanned files."""

from __future__ import annotations

from ..job import JobContext, Runner


def run(ctx: JobContext) -> None:
    from ...metadata import enrich as enrich_mod

    conn, job = ctx.conn, ctx.job
    prog = ctx.progress()
    root_ids = (job.root_id,) if job.root_id else None
    stats = enrich_mod.enrich(conn, ctx.cfg, progress=prog, root_ids=root_ids)
    job.message = (
        f"{stats.processed} processed, "
        f"{stats.with_takeout} Takeout-matched, "
        f"{stats.with_gps} with GPS"
    )


RUNNER = Runner(kind="enrich", run=run, takes_write_lock=False)
