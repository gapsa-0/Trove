"""The text stage: read what is inside a file, and index it.

Manages its own connections rather than taking the manager's
(``needs_connection=False``), for the reason ``semantic.py`` gives: the pass
snapshots its backlog under a read-only connection and then opens a fresh one
per result, so the long parsing work happens without the writer's lock held and
metadata, duplicates and detection keep moving alongside it.

One pass serves two features. Which halves are on is resolved once per pass and
threaded through, because it decides both what enters the backlog and what is
recorded on every row -- a file read with one half on becomes work again when
the other is switched on, and nothing else in the pipeline would notice.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

from ... import features
from ...config import Config
from ...db import database as db
from ...text import extract
from ...text.chunk import chunk_blocks
from ..job import Job, JobContext, Runner

logger = logging.getLogger(__name__)


def run(ctx: JobContext) -> None:
    """Drain the backlog in passes, so this stays one continuous job.

    Each pass handles the backlog it snapshotted, then loops to pick up whatever
    became pending while it ran -- files the concurrent scan just added. Starting
    a fresh job per snapshot instead is what makes a card flicker done→running;
    looping here keeps it steadily running until there is genuinely nothing left.
    """
    job = ctx.job
    read = written = skipped = failed = 0
    force = job.force
    while True:
        ctx.raise_if_cancelled()
        got, wrote, skip, fail, remaining = _text_pass(ctx.cfg, job, ctx.cancel, force)
        read += got
        written += wrote
        skipped += skip
        failed += fail
        if remaining == 0:
            break
        force = False  # only the first pass honours a forced re-read
    job.message = (
        f"{read} read, {written:,} passages indexed, {skipped} skipped, {failed} errors"
        if (read or skipped or failed)
        else "every document has been read"
    )


def _text_pass(
    cfg: Config, job: Job, cancel: threading.Event, force: bool
) -> tuple[int, int, int, int, int]:
    """One snapshot pass. Returns (read, chunks, skipped, failed, rows_in_pass)."""
    from ...services import documents

    root_id = job.require_root()
    extractors = features.extractors(cfg.archive_features(root_id))
    db_path = cfg.archive_db_path(root_id)
    rows = documents.pending_rows(db_path, root_id, extractors, force=force)
    total, already = documents.work_counts(db_path, root_id, extractors, force=force)
    job.total, job.done = total, already
    if not rows:
        return (0, 0, 0, 0, 0)

    wanted = documents.wanted_key(extractors)
    # Resolved once per pass rather than per file: trove/text is L1 and knows
    # nothing about an archive's settings, so this layer hands them down.
    limits = extract.Limits(
        max_bytes=cfg.documents_max_bytes,
        max_pages=cfg.ocr_max_pages_per_file,
        render_dpi=cfg.ocr_render_dpi,
        detect_side=cfg.ocr_detect_side,
        min_chars_per_page=cfg.ocr_text_layer_chars_per_page,
        min_image_cover=cfg.ocr_min_image_cover,
    )
    read = written = skipped = failed = 0
    for offset, row in enumerate(rows):
        if cancel.is_set():
            raise KeyboardInterrupt
        job.current = row["rel_path"]
        try:
            extraction = extract.read(
                Path(row["root_path"]) / row["rel_path"],
                row["ext"],
                row["media_type"],
                extractors,
                limits=limits,
            )
            chunks = chunk_blocks(
                list(extraction.blocks),
                target=cfg.documents_chunk_chars,
                overlap=cfg.documents_chunk_overlap,
            )
            _save(cfg, root_id, row, extraction, chunks, wanted, None)
            read += 1
            written += len(chunks)
        except KeyboardInterrupt:
            raise
        except ValueError as exc:
            # The readers' own vocabulary: an unreadable format, an oversized
            # file, a scan with no text layer. Expected outcomes, recorded per
            # file, and not worth a log line each across a whole archive.
            _save(cfg, root_id, row, None, [], wanted, str(exc))
            skipped += 1
        except Exception as exc:
            # One line, no traceback: this loop covers every document in the
            # archive, and the reason is persisted on the row anyway.
            logger.warning("reading text failed for file_id=%s: %s", row["id"], exc)
            _save(cfg, root_id, row, None, [], wanted, str(exc))
            failed += 1
        job.done = already + offset + 1
    return (read, written, skipped, failed, len(rows))


def _save(
    cfg: Config,
    root_id: int,
    row: sqlite3.Row,
    extraction: object,
    chunks: list,
    wanted: str,
    error: str | None,
) -> None:
    """Commit one file's outcome in its own small transaction.

    Re-checks the file first. Dedup can run while a long document is being
    parsed, and writing text for a copy that has since been hidden -- or for
    bytes that have since changed -- would put a passage in the index that
    belongs to nothing anyone can browse to.

    A lock that outlasts the retries is not a stage failure. The row is simply
    left pending and the next pass picks it up, which is the whole reason the
    outcome is written per file rather than per batch.
    """
    from ...services import documents

    conn = db.connect(cfg.archive_db_path(root_id))
    try:
        current = conn.execute(
            "SELECT hidden, sha256 FROM files WHERE id=?", (row["id"],)
        ).fetchone()
        if current is None or current["hidden"] or current["sha256"] != row["sha256"]:
            return

        def _write() -> None:
            documents.save_outcome(conn, row, extraction, chunks, wanted, error)  # type: ignore[arg-type]
            conn.commit()

        try:
            db.write_with_retry(_write)
        except sqlite3.OperationalError:
            conn.rollback()
    finally:
        conn.close()


RUNNER = Runner(kind="text", run=run, takes_write_lock=False, needs_connection=False)
