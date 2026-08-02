"""The dedup stage: rebuild duplicate groups and pick a canonical file each."""

from __future__ import annotations

from ...db import database as db
from ..job import JobContext, Runner


def run(ctx: JobContext) -> None:
    from ...dedup import exact

    conn, job = ctx.require_conn(), ctx.job
    # dedup is only ever started by the scheduler, always with the currently
    # open root's id -- see scan.py's comment for the same invariant.
    root_id = job.require_root()
    prog = ctx.progress()
    stats = exact.run(conn, ctx.cfg, progress=prog, root_id=root_id)
    # Hidden files are duplicate copies. They must never consume semantic
    # storage or appear as a stale vector if a prior run overlapped dedup.
    conn.execute(
        "DELETE FROM semantic_embeddings WHERE file_id IN (SELECT id FROM files WHERE hidden=1)"
    )
    # Record what this successful rebuild covered so dedup_needed() can
    # tell -- from the catalog alone, even after a restart -- that nothing
    # is owed, until a later scan/enrich invalidates it again
    # (_mark_dedup_owed). Sharing this commit with the DELETE above (not
    # exact.run()'s own, earlier commit) is fine: if the process dies
    # between them, the grouping already landed correctly and the only
    # cost is one redundant, harmless re-run that re-derives the same
    # grouping and then marks it.
    covered_files, covered_max_id = db.dedup_coverage(conn, root_id)
    db.dedup_mark_done(conn, root_id, covered_files, covered_max_id)
    conn.commit()
    job.message = (
        f"{stats.groups} groups, {stats.duplicate_files} duplicates, "
        f"{stats.reclaimable_bytes / 1e9:.1f} GB reclaimable"
    )


RUNNER = Runner(kind="dedup", run=run)
