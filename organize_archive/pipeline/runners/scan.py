"""The scan stage: walk the source root(s) and update the file catalog."""

from __future__ import annotations

from ...db import database as db
from ...scan import walker
from ..job import JobContext, Runner


def run(ctx: JobContext) -> None:
    from pathlib import Path

    cfg, conn, job = ctx.cfg, ctx.conn, ctx.job
    prog = ctx.progress()
    run_started = db.now_iso()
    # An archive database has exactly one root; job.root_path is always
    # supplied by the scheduler, this is just a defensive fallback.
    roots = [job.root_path] if job.root_path else [cfg.archive_path(job.root_id)]
    on_disk = sum(walker.count_files(Path(r)) for r in roots if Path(r).is_dir())
    prog.total = on_disk
    run_id = db.scan_run_start(conn, job.root_id, roots)
    totals = walker.ScanStats()
    for r in roots:
        stats = walker.scan_root(
            conn,
            cfg,
            r,
            run_started,
            progress=prog,
            base_done=totals.seen,
            # Small batches so the parallel enrich job
            # can begin reading committed rows quickly.
            commit_every=80,
            # This archive's root id, not a path lookup:
            # the rows must land where the GUI reads.
            root_id=job.root_id,
        )
        totals.seen += stats.seen
        totals.new += stats.new
        totals.updated += stats.updated
        totals.errors += stats.errors
        totals.bytes_hashed += stats.bytes_hashed
    # Reached only when every root was walked end to end: cancellation and
    # errors both leave the run open, so neither can pass for full coverage.
    db.scan_run_finish(conn, run_id, totals, on_disk)
    job.message = f"{totals.seen} files scanned" + (
        f" · {totals.errors} unreadable" if totals.errors else ""
    )


RUNNER = Runner(kind="scan", run=run, takes_write_lock=False)
