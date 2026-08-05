"""The meaning stage: embed document passages so they can be found by sense.

Manages its own connections (``needs_connection=False``), like semantic and
text: the backlog is snapshotted under a read-only connection and each
document's vectors are written in their own small transaction, so hours of
inference never hold the writer's lock.

**It depends on ``dedup``, not on ``text``.** A stage may not depend on one an
archive can switch off (``tests/unit/test_features.py``), and it does not need
to: this stands to the text stage exactly as semantic stands to scan — no
declared dependency, a backlog that is simply zero until passages exist, and the
drain loop below picking them up as they arrive. The visible cost is that this
card can read done, then queued again ten seconds later, while the text stage is
still committing. That is already true of scan and semantic today.
"""

from __future__ import annotations

import logging
import sqlite3
import threading

from ...config import Config
from ...db import database as db
from ..job import Job, JobContext, Runner

logger = logging.getLogger(__name__)


def run(ctx: JobContext) -> None:
    """Drain the backlog in passes, so this stays one continuous job."""
    job = ctx.job
    _warm_model(ctx)
    documents = passages = failed = 0
    force = job.force
    while True:
        ctx.raise_if_cancelled()
        docs, vectors, errors, remaining = _meaning_pass(ctx.cfg, job, ctx.cancel, force)
        documents += docs
        passages += vectors
        failed += errors
        if remaining == 0:
            break
        force = False  # only the first pass honours a forced re-embed
    job.message = (
        f"{documents} documents, {passages:,} passages indexed"
        + (f", {failed} errors" if failed else "")
        if (documents or failed)
        else "every document has been indexed for meaning"
    )


def _warm_model(ctx: JobContext) -> None:
    """Load the embedder before the loop, reporting the download on the card.

    Failure is swallowed on purpose: the pass records it per document, and a
    stage that raised here would take its whole backlog down with it rather
    than the one document that could not be read.
    """
    from ...services import meaning

    if not meaning.available():
        return
    with (
        ctx.preparing("loading the meaning model"),
        ctx.uninterruptible("loading the meaning model"),
    ):
        try:
            meaning.backend(ctx.cfg).load(log=lambda m: setattr(ctx.job, "current", m))
        except Exception:
            logger.debug("meaning warm-up failed; the pass reports it per document", exc_info=True)


def _meaning_pass(
    cfg: Config, job: Job, cancel: threading.Event, force: bool
) -> tuple[int, int, int, int]:
    """One snapshot pass. Returns (documents, passages, failed, rows_in_pass)."""
    from ...services import meaning

    root_id = job.require_root()
    db_path = cfg.archive_db_path(root_id)
    file_ids = meaning.pending_documents(db_path, root_id, force=force)
    total, already = meaning.work_counts(db_path, root_id, force=force)
    job.total, job.done = total, already
    if not file_ids:
        return (0, 0, 0, 0)

    documents = passages = failed = 0
    for offset, file_id in enumerate(file_ids):
        if cancel.is_set():
            raise KeyboardInterrupt
        try:
            chunks = meaning.chunk_texts(db_path, file_id)
            if chunks:
                job.current = f"{len(chunks)} passages"
                vectors = meaning.backend(cfg).embed_passages([text for _, text in chunks])
                written = _save(
                    cfg, root_id, file_id, list(zip([c for c, _ in chunks], vectors, strict=True))
                )
                passages += written
                documents += 1
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            # One line, no traceback: this loop covers every document in the
            # archive. Unlike the text stage there is no per-file outcome row to
            # record a reason on -- a document that cannot be embedded simply
            # stays pending and is retried, which is right for a failure that is
            # far more likely to be transient (a locked file, memory pressure)
            # than a property of the text.
            logger.warning("embedding failed for file_id=%s: %s", file_id, exc)
            failed += 1
        job.done = already + offset + 1
    return (documents, passages, failed, len(file_ids))


def _save(cfg: Config, root_id: int, file_id: int, vectors: list[tuple[int, list[float]]]) -> int:
    """Commit one document's vectors in their own transaction. Returns how many.

    Re-checks that the passages still exist and still belong to a visible file:
    the text stage can re-read a document while this one is embedding it, which
    replaces its chunks wholesale, and writing against the ids that were read at
    the start would attach vectors to passages that no longer exist.

    A lock outlasting the retries is not a stage failure -- the document stays
    pending and the next pass picks it up.
    """
    from ...services import meaning

    conn = db.connect(cfg.archive_db_path(root_id))
    try:
        current = conn.execute(
            """SELECT COUNT(*) FROM doc_chunks c JOIN files f ON f.id=c.file_id
                WHERE c.file_id=? AND f.present=1 AND f.hidden=0""",
            (file_id,),
        ).fetchone()[0]
        if current != len(vectors):
            return 0

        def _write() -> None:
            meaning.save_embeddings(conn, vectors)
            conn.commit()

        try:
            db.write_with_retry(_write)
        except sqlite3.OperationalError:
            conn.rollback()
            return 0
        return len(vectors)
    finally:
        conn.close()


RUNNER = Runner(kind="meaning", run=run, takes_write_lock=False, needs_connection=False)
