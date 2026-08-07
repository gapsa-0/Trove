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
    # on a cold cache over ~150k files that is minutes on its own. The scheduler
    # has already paid for it -- it cannot decide a scan is owed without asking
    # the same question -- so the usual path is handed the answer and walks
    # once, not twice. Say so rather than showing an empty bar for the count
    # that is still needed when nobody supplied one, which is a job started by
    # hand or from a test (see JobContext.preparing).
    if job.files_on_disk is None:
        with ctx.preparing("counting files on disk"):
            on_disk = sum(walker.count_files(Path(r)) for r in roots if Path(r).is_dir())
    else:
        on_disk = job.files_on_disk
    prog.total = on_disk
    # How much of this walk is ground already covered. Unlike every other
    # stage, scan restarts at the top of the tree rather than at its own
    # backlog, and re-reaching what it already holds costs a stat() and one
    # UPDATE per file -- fast enough that counting it as progress made the
    # bar rewind and race back to where it stopped. Below this the card
    # says what is happening instead of drawing a bar (Job.recheck_below).
    #
    # Clamped to the disk count: files deleted since the last run are in
    # the catalogue and not in this walk, and a mark past the end of the
    # bar would leave it hidden for the whole run.
    job.recheck_below = min(db.present_file_count(conn, root_id), on_disk)
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
        totals.unstable += stats.unstable
    # Reached only when every root was walked end to end: cancellation and
    # errors both leave the run open, so neither can pass for full coverage.
    # Files still being copied are recorded rather than hidden -- the walk did
    # finish, and scan_settled is what decides that finishing it was not the
    # same as covering it.
    db.scan_run_finish(conn, run_id, totals, on_disk)
    job.message = (
        f"{totals.seen} files scanned"
        + (f" · {totals.errors} unreadable" if totals.errors else "")
        # Said plainly, because the alternative is an archive that looks
        # finished while a video someone is still copying into it is missing.
        + (f" · {totals.unstable} still being copied" if totals.unstable else "")
    )


RUNNER = Runner(kind="scan", run=run, takes_write_lock=False)
