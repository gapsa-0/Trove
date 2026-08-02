"""The semantic stage: embed media into the local vision index for search.

Semantic manages its own connections instead of taking the one the manager
opens (``needs_connection=False``): ``_semantic_pass`` snapshots the backlog
under a read-only connection, and ``_save_semantic_outcome`` opens a fresh
one per result. ``ctx.conn`` is therefore unused here; ``_semantic_pass`` and
``_save_semantic_outcome`` are module-level so they take ``cfg`` explicitly
rather than closing over a manager instance.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from typing import cast

from ...config import Config
from ...db import database as db
from ..job import Job, JobContext, Runner

logger = logging.getLogger(__name__)


def run(ctx: JobContext) -> None:
    # Drain in passes so semantic indexing is ONE continuous job: each pass
    # handles the current backlog, then loops to pick up anything that became
    # pending while it ran (files the concurrent scan/enrich just added).
    # Restarting a fresh job per snapshot is what used to flicker the card
    # done→running; looping here keeps it steadily "running" until drained.
    job = ctx.job
    _warm_vision_model(ctx)
    total_indexed = total_skipped = total_failed = 0
    force = job.force
    while True:
        ctx.raise_if_cancelled()
        indexed, skipped, failed, remaining = _semantic_pass(ctx.cfg, job, ctx.cancel, force)
        total_indexed += indexed
        total_skipped += skipped
        total_failed += failed
        if remaining == 0:
            break
        force = False  # only the first pass honours a forced full reindex
    job.message = (
        f"{total_indexed} indexed, {total_skipped} skipped, {total_failed} errors"
        if (total_indexed or total_skipped or total_failed)
        else "semantic index is already current"
    )


def _warm_vision_model(ctx: JobContext) -> None:
    """Load the vision tower before the drain loop starts.

    It would otherwise load inside the first embed, where the seconds spent in
    native code cannot see a cancel and shutdown would wait out its whole
    timeout. Hoisting it puts that call in a window shutdown knows to skip.

    Failure stays quiet on purpose: ``_semantic_pass`` already records an
    unloadable backend as a per-file reason, and pulling the load forward must
    not promote that to a failure of the whole job.
    """
    from ...services import semantic

    if not semantic.available():
        return
    with ctx.uninterruptible("loading the semantic model"):
        try:
            semantic.backend(ctx.cfg, log=lambda m: setattr(ctx.job, "current", m)).load_vision()
        except Exception:
            logger.debug("semantic warm-up failed; the pass reports it per file", exc_info=True)


def _semantic_pass(
    cfg: Config, job: Job, cancel: threading.Event, force: bool
) -> tuple[int, int, int, int]:
    """One snapshot pass. Returns (indexed, skipped, failed, rows_in_pass)."""
    from pathlib import Path

    from ...services import semantic

    # semantic is only ever started by the scheduler, always with the
    # currently open root's id -- see scan.py's comment for the same
    # invariant (needs_connection=False does not change who starts it).
    root_id = cast(int, job.root_id)
    # Snapshot candidates under a read-only connection. The API calls below
    # happen without the writer lock so local metadata/faces work continues.
    read_conn = db.open_readonly(cfg.archive_db_path(root_id))
    try:
        rows = semantic.pending_rows(read_conn, root_id, force=force)
        total, already = semantic.work_counts(read_conn, root_id, force=force)
    finally:
        read_conn.close()
    job.total, job.done = total, already
    if not rows:
        return (0, 0, 0, 0)
    indexed = skipped = failed = 0
    # A straight loop, one file at a time. The old batch-then-isolate retry
    # ladder existed because a single malformed input could 400 an entire
    # Voyage request and take its innocent neighbours down with it. Local
    # inference has no such failure mode: a file either decodes or it does
    # not, and media_part has already decided that. Batching would not even
    # pay for itself — on this CPU a batch of four costs four single
    # forwards — while one-at-a-time keeps the progress card truthful.
    # Per-item error capture stays: a truncated JPEG can still raise inside
    # PIL, and that must cost one file, not the pass.
    cache_dir = cfg.archive_cache_dir(root_id)
    for offset, row in enumerate(rows):
        if cancel.is_set():
            raise KeyboardInterrupt
        job.current = row["rel_path"]
        try:
            part, kind, reason = semantic.media_part(
                cfg,
                Path(row["root_path"]) / row["rel_path"],
                row["ext"],
                row["media_type"],
                cache_dir,
                row["rotate_deg"],
                row["duration_s"],
            )
            if reason:
                _save_semantic_outcome(cfg, root_id, row, None, kind, reason)
                skipped += 1
            else:
                # media_part's invariant: part is None iff reason is set, just
                # checked above (mirrors services/semantic.py's embed_media).
                values = semantic.embed_part(cfg, cast(list[Path], part), kind)
                if values is None:
                    _save_semantic_outcome(
                        cfg,
                        root_id,
                        row,
                        None,
                        kind,
                        f"unsupported {row['media_type']}: no frame could be decoded",
                    )
                    skipped += 1
                else:
                    _save_semantic_outcome(cfg, root_id, row, values, kind, None)
                    indexed += 1
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            # One line, no exc_info: this loop covers every file in the
            # archive (~150k), so a traceback each would fill the rotated log
            # before anything useful survived. The reason is also persisted
            # per row by _save_semantic_outcome, which the GUI surfaces.
            logger.warning("semantic indexing failed for file_id=%s: %s", row["id"], exc)
            _save_semantic_outcome(cfg, root_id, row, None, None, str(exc))
            failed += 1
        job.done = already + offset + 1
    return (indexed, skipped, failed, len(rows))


def _save_semantic_outcome(
    cfg: Config,
    root_id: int,
    row: sqlite3.Row,
    values: list[float] | None,
    kind: str | None,
    reason: str | None,
) -> None:
    """Persist one result without taking the manager-wide pipeline lock.

    SQLite WAL plus its busy timeout serializes this tiny transaction against
    a metadata/faces batch, while the semantic worker remains independent of
    that job's long-lived manager lock. ``write_with_retry`` covers the rarer
    case where that still isn't enough (a batch write holding the writer past
    busy_timeout, see detect's commit budget); if the lock still won't clear,
    this row is left pending instead of turning into the card's error text --
    a lock is not a stage failure, and the next semantic pass will retry it
    since no embedding was ever written for it.
    """
    from ...services import semantic

    conn = db.connect(cfg.archive_db_path(root_id))
    try:
        # Dedup may have completed while this item was being embedded.
        # Only keep an outcome if this exact source remains canonical.
        current = conn.execute(
            "SELECT hidden, sha256 FROM files WHERE id=?", (row["id"],)
        ).fetchone()
        if current is None or current["hidden"] or current["sha256"] != row["sha256"]:
            return

        def _write() -> None:
            semantic.save_outcome(conn, cfg, row, values, kind, reason)
            conn.commit()

        try:
            db.write_with_retry(_write)
        except sqlite3.OperationalError:
            conn.rollback()
    finally:
        conn.close()


RUNNER = Runner(kind="semantic", run=run, takes_write_lock=False, needs_connection=False)
