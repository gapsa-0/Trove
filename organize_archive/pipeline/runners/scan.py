"""The scan stage: walk the source root(s) and update the file catalog."""

from __future__ import annotations

from typing import cast

from ...db import database as db
from ...scan import walker
from ..job import JobContext, Runner


def run(ctx: JobContext) -> None:
    from pathlib import Path

    cfg, conn, job = ctx.cfg, ctx.require_conn(), ctx.job
    prog = ctx.progress()
    run_started = db.now_iso()
    # scan is only ever started by the scheduler (scheduler.tick), which
    # always supplies an open root's id -- see JobManager.start's root_id
    # check. root_id is int here even though Job.root_id is optional for
    # face_cluster/pet_cluster, which the scheduler never starts.
    root_id = job.require_root()
    # An archive database has exactly one root; job.root_path is always
    # supplied by the scheduler, this is just a defensive fallback.
    roots: list[str] = [job.root_path] if job.root_path else [cast(str, cfg.archive_path(root_id))]
    # Counting is a full walk of the tree before a single file is scanned, and
    # on a cold cache over ~150k files that is minutes on its own. Say so
    # rather than showing an empty bar for it (see JobContext.preparing).
    with ctx.preparing("counting files on disk"):
        on_disk = sum(walker.count_files(Path(r)) for r in roots if Path(r).is_dir())
        prog.total = on_disk
    run_id = db.scan_run_start(conn, root_id, roots)
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
            root_id=root_id,
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
