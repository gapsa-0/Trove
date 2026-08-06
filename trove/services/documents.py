"""Deciding which files the text pass reads, and recording what it found.

The read half of searching inside documents lives in ``services/text_search.py``;
this is the write half. It mirrors ``services/semantic.py`` function for
function, deliberately: a reader who knows how the semantic index stays
resumable knows how this one does, because it is the same four pieces --
``pending_rows`` and ``work_counts`` asking the same question two ways,
``save_outcome`` closing the loop, and a version constant that re-queues the
archive when the algorithm changes.

**One leg is new, and it is the one the fused stage needs.** Search by document
text and Search by picture text share a pass, so a file's row records which
halves were switched on when it was read (``doc_text.wanted``). Without that, a
scan read once with only the document half on carries a current hash and a
current version, is therefore never pending, and switching the picture half on
afterwards would never bring it back -- the file would simply never be read,
silently, forever.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..db import database as db
from ..text import extract
from ..text.results import CLEAN_SKIP_PREFIXES, Chunk, Extraction
from ._common import _NOT_HIDDEN, reading

# Identity of what ``doc_text`` holds. Bump when a reader, the chunker or the
# chunk sizes change: every row stamped with an older value describes text this
# build would no longer produce, so the archive re-reads itself. The same job
# ``INDEXER_VERSION`` does for embeddings, and the reason neither needs a
# migration to change its mind.
TEXT_VERSION = "doctext-v1"


def available(extractors: frozenset[str]) -> bool:
    """Whether the text stage can run at all for this feature set.

    Two questions, both about what is present rather than what is downloaded:
    can anything read the files, and is there an index to put the result in.
    FTS5 is the one that can genuinely be absent, and a build without it reports
    the feature unavailable rather than failing a migration for every archive.
    """
    return bool(extractors) and extract.available(extractors) and db.fts5_supported()


def wanted_key(extractors: frozenset[str]) -> str:
    """The feature set as one sortable string, for ``doc_text.wanted``.

    Sorted so that the same pair of halves always produces the same key -- a set
    has no order, and a key that varied would make every file pending on every
    pass.
    """
    return "+".join(sorted(extractors))


def _pending_clause(wanted: str) -> tuple[str, list[Any]]:
    """The four ways a file can owe the text stage work.

    Never read; read at different bytes; read by an older version of this
    algorithm; or read while a different set of halves was switched on. The
    fourth is what makes enabling the picture half actually revisit the scans
    that the document half could not read.
    """
    return (
        """(t.file_id IS NULL
             OR t.source_sha256 IS NOT f.sha256
             OR COALESCE(t.text_version, '') != ?
             OR t.wanted IS NOT ?)""",
        [TEXT_VERSION, wanted],
    )


def _candidate_exts(extractors: frozenset[str]) -> tuple[str, list[Any]]:
    """SQL restricting the backlog to files something switched on could read.

    With nothing switched on the answer is "no file", spelled as a false literal
    rather than as ``IN ()`` -- an empty IN list is a syntax error in SQLite, not
    an empty result. The stage is never scheduled in that state, so this is a
    guard against a direct call rather than a path the pipeline takes.
    """
    exts = sorted(extract.readable_exts(extractors))
    if not exts:
        return "0", []
    placeholders = ",".join("?" for _ in exts)
    return f"f.ext IN ({placeholders})", list(exts)


@reading
def pending_rows(
    conn: sqlite3.Connection,
    root_id: int | None,
    extractors: frozenset[str],
    *,
    force: bool = False,
) -> list[sqlite3.Row]:
    """Files the text stage still owes work on, oldest id first.

    ``ORDER BY f.id`` rather than by anything meaningful: it is stable, so an
    interrupted pass resumes in the same order it left off instead of
    re-shuffling the backlog every time the job restarts.
    """
    wanted = wanted_key(extractors)
    ext_sql, ext_params = _candidate_exts(extractors)
    where = [_NOT_HIDDEN, ext_sql]
    params: list[Any] = list(ext_params)
    if root_id is not None:
        where.append("f.root_id=?")
        params.append(root_id)
    if not force:
        clause, clause_params = _pending_clause(wanted)
        where.append(clause)
        params += clause_params
    rows = conn.execute(
        f"""SELECT f.id, f.rel_path, f.ext, f.media_type, f.sha256, r.path AS root_path
              FROM files f
              JOIN roots r ON r.id=f.root_id
              LEFT JOIN doc_text t ON t.file_id=f.id
             WHERE {" AND ".join(where)}
             ORDER BY f.id""",
        params,
    ).fetchall()
    return list(rows)


def _counts(
    conn: sqlite3.Connection,
    root_id: int | None,
    extractors: frozenset[str],
    force: bool = False,
) -> tuple[int, int]:
    """``(total, already done)``, asked as the complement of the backlog.

    Undecorated so the two public callers below can share it: one hands out the
    pair for a progress bar, the other subtracts it for a card. Recomputing the
    predicate in each would let the bar and the backlog drift apart, which is
    the one thing a progress bar must never do.
    """
    wanted = wanted_key(extractors)
    ext_sql, ext_params = _candidate_exts(extractors)
    where = [_NOT_HIDDEN, ext_sql]
    params: list[Any] = list(ext_params)
    if root_id is not None:
        where.append("f.root_id=?")
        params.append(root_id)
    total = conn.execute(
        f"SELECT COUNT(*) FROM files f WHERE {' AND '.join(where)}", params
    ).fetchone()[0]
    if force:
        return int(total), 0
    done = conn.execute(
        f"""SELECT COUNT(*) FROM files f JOIN doc_text t ON t.file_id=f.id
             WHERE {" AND ".join(where)}
               AND t.source_sha256 IS f.sha256
               AND COALESCE(t.text_version, '') = ?
               AND t.wanted IS ?""",
        (*params, TEXT_VERSION, wanted),
    ).fetchone()[0]
    return int(total), int(done)


@reading
def work_counts(
    conn: sqlite3.Connection,
    root_id: int | None,
    extractors: frozenset[str],
    *,
    force: bool = False,
) -> tuple[int, int]:
    """``(total, already done)`` for the runner's progress bar."""
    return _counts(conn, root_id, extractors, force)


@reading
def text_pending(conn: sqlite3.Connection, root_id: int | None, extractors: frozenset[str]) -> int:
    """How many files the text stage still owes, for the Overview card."""
    total, done = _counts(conn, root_id, extractors)
    return max(0, total - done)


def is_clean_skip(error: str | None) -> bool:
    """Whether a reason describes a normal outcome rather than a failure.

    It decides the label on the row and nothing else. A row is written either
    way, carrying the hash and feature set it was produced under, so both a skip
    and an error stop the file being pending; only ``_pending_clause`` ever
    re-queues anything. The distinction exists so that an archive of scans does
    not show a red error count for working exactly as designed.
    """
    if not error:
        return False
    return error.startswith(CLEAN_SKIP_PREFIXES)


def clear_chunks(conn: sqlite3.Connection, file_id: int) -> None:
    """Drop a file's chunks and the index rows addressing them.

    The index has to go first and by hand. ``doc_chunk_fts`` is a virtual table,
    so no foreign key reaches it, and a cascade would not fire a trigger anyway
    while ``recursive_triggers`` is off (``db/migrations.py``). Deleting the
    chunks first would leave the index addressing rows that are gone.
    """
    if db.text_index_present(conn):
        conn.execute(
            "DELETE FROM doc_chunk_fts WHERE rowid IN (SELECT id FROM doc_chunks WHERE file_id=?)",
            (file_id,),
        )
    conn.execute("DELETE FROM doc_chunks WHERE file_id=?", (file_id,))


def save_chunks(conn: sqlite3.Connection, file_id: int, chunks: list[Chunk]) -> None:
    """Replace a file's chunks wholesale, keeping the index in step.

    Wholesale because a re-read is a new reading of the whole file: chunk
    boundaries move when the text changes, so merging generations would leave
    passages that no longer exist anywhere in the document. Does not commit --
    the caller owns the transaction, as everywhere in this layer.
    """
    clear_chunks(conn, file_id)
    indexed = db.text_index_present(conn)
    for chunk in chunks:
        cursor = conn.execute(
            """INSERT INTO doc_chunks(file_id, ordinal, page_first, page_last, chars)
               VALUES(?,?,?,?,?)""",
            (file_id, chunk.ordinal, chunk.page_first, chunk.page_last, len(chunk.text)),
        )
        if indexed:
            conn.execute(
                "INSERT INTO doc_chunk_fts(rowid, text) VALUES(?, ?)",
                (cursor.lastrowid, chunk.text),
            )


def save_outcome(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    extraction: Extraction | None,
    chunks: list[Chunk],
    wanted: str,
    error: str | None,
) -> None:
    """Record what reading one file produced, chunks included.

    A failed or skipped file gets a row too, which is what makes the pass
    resumable: without it the same unreadable file would be re-derived on every
    pass forever. Does not commit.
    """
    status = "extracted" if extraction else ("skipped" if is_clean_skip(error) else "error")
    if extraction:
        save_chunks(conn, row["id"], chunks)
    else:
        # Nothing readable now, so nothing indexed from before may survive: a
        # file that used to have text and no longer does must stop matching.
        clear_chunks(conn, row["id"])
    conn.execute(
        """INSERT INTO doc_text(file_id, source_sha256, wanted, extractor, status,
                                confidence, chars, pages, n_chunks, error,
                                text_version, extracted_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(file_id) DO UPDATE SET
               source_sha256=excluded.source_sha256, wanted=excluded.wanted,
               extractor=excluded.extractor, status=excluded.status,
               confidence=excluded.confidence, chars=excluded.chars,
               pages=excluded.pages, n_chunks=excluded.n_chunks,
               error=excluded.error, text_version=excluded.text_version,
               extracted_at=excluded.extracted_at""",
        (
            row["id"],
            row["sha256"] or "",
            wanted,
            extraction.extractor if extraction else None,
            status,
            extraction.confidence if extraction else None,
            extraction.chars if extraction else 0,
            extraction.pages if extraction else None,
            len(chunks),
            error,
            TEXT_VERSION,
            db.now_iso(),
        ),
    )
