"""Deciding which passages get a vector, and recording them.

The write half of searching documents by meaning. Same four pieces as
``services/documents.py`` and ``services/semantic.py`` — a backlog query, its
complement as counts, a save that closes the loop, and a version constant that
re-queues the archive when the algorithm changes.

**The unit here is a document, not a passage**, everywhere the pipeline can see.
The Overview card renders its backlog as "{n} items queued", and 4,812 items
meaning 4,812 passages across sixty PDFs is a lie about how much is left. So the
backlog counts documents, the runner iterates documents, and a document's
passages are embedded and written together — which is also the shape the model
wants, since a batched forward over one document's chunks costs far less than
one call each.
"""

from __future__ import annotations

import sqlite3
import struct
import threading
from typing import Any

from ..config import Config
from ..db import database as db
from ..embeddings import text_backend as etb
from ._common import _NOT_HIDDEN, reading

# Identity of the vector space ``doc_chunk_embeddings`` holds. Tied to the
# embedder's own version rather than restated, so there is one place to change.
MEANING_VERSION = etb.EMBEDDER_VERSION

_backend: etb.E5Backend | None = None
_backend_lock = threading.Lock()


def available() -> bool:
    """Whether this build can embed text at all."""
    return etb.available()


def models_ready(cfg: Config) -> bool:
    """Whether the weights are already on disk."""
    return etb.models_ready(cfg.cache_dir)


def backend(cfg: Config) -> etb.E5Backend:
    """The process-wide embedder, created once.

    One session for the whole process rather than one per job: it is ~118 MB
    resident, and the indexing stage and a search would otherwise hold two.
    """
    global _backend
    with _backend_lock:
        if _backend is None:
            _backend = etb.E5Backend(cfg.cache_dir)
        return _backend


def embed_queries(cfg: Config, queries: list[str]) -> list[list[float]]:
    """Vectors for typed searches, in the query half of the space."""
    return backend(cfg).embed_queries(queries)


def _eligible(root_id: int | None) -> tuple[str, list[Any]]:
    """Passages this archive could embed: those belonging to visible files."""
    where = [_NOT_HIDDEN]
    params: list[Any] = []
    if root_id is not None:
        where.append("f.root_id=?")
        params.append(root_id)
    return " AND ".join(where), params


def _counts(conn: sqlite3.Connection, root_id: int | None, force: bool = False) -> tuple[int, int]:
    """``(documents with passages, documents fully embedded)``.

    Undecorated so the runner's progress bar and the Overview's card share one
    predicate; recomputing it in each is how a bar and a backlog drift apart.
    """
    where, params = _eligible(root_id)
    total = conn.execute(
        f"""SELECT COUNT(DISTINCT c.file_id) FROM doc_chunks c
              JOIN files f ON f.id=c.file_id
             WHERE {where}""",
        params,
    ).fetchone()[0]
    if force:
        return int(total), 0
    # A document counts as done only when *every* passage of it has a current
    # vector. Counting any-vector would call a half-embedded document finished
    # and leave the rest of it unsearchable with the card reading complete.
    done = conn.execute(
        f"""SELECT COUNT(*) FROM (
                SELECT c.file_id FROM doc_chunks c
                  JOIN files f ON f.id=c.file_id
                  LEFT JOIN doc_chunk_embeddings e
                         ON e.chunk_id=c.id AND e.embedder_version=?
                 WHERE {where}
                 GROUP BY c.file_id
                HAVING COUNT(*) = COUNT(e.chunk_id))""",
        (MEANING_VERSION, *params),
    ).fetchone()[0]
    return int(total), int(done)


@reading
def work_counts(
    conn: sqlite3.Connection, root_id: int | None, *, force: bool = False
) -> tuple[int, int]:
    """``(total, already done)`` in documents, for the runner's progress bar."""
    return _counts(conn, root_id, force)


@reading
def meaning_pending(conn: sqlite3.Connection, root_id: int | None) -> int:
    """Documents still owed vectors, for the Overview card."""
    total, done = _counts(conn, root_id)
    return max(0, total - done)


@reading
def pending_documents(
    conn: sqlite3.Connection, root_id: int | None, *, force: bool = False, limit: int = 500
) -> list[int]:
    """File ids with at least one passage lacking a current vector, oldest first.

    Bounded, unlike the other stages' backlog queries: a pass holds every
    returned document's text in memory while it embeds, so the snapshot is a
    working set rather than the whole archive. The runner loops until drained.
    """
    where, params = _eligible(root_id)
    stale = "" if force else " AND e.chunk_id IS NULL"
    rows = conn.execute(
        f"""SELECT DISTINCT c.file_id FROM doc_chunks c
              JOIN files f ON f.id=c.file_id
              LEFT JOIN doc_chunk_embeddings e
                     ON e.chunk_id=c.id AND e.embedder_version=?
             WHERE {where}{stale}
             ORDER BY c.file_id
             LIMIT ?""",
        (MEANING_VERSION, *params, limit),
    ).fetchall()
    return [int(r[0]) for r in rows]


@reading
def chunk_texts(conn: sqlite3.Connection, file_id: int) -> list[tuple[int, str]]:
    """``(chunk_id, text)`` for one document, in reading order.

    The text comes back out of the full-text index, which is where it lives:
    ``doc_chunks`` holds only metadata, so there is one copy of a passage rather
    than two.
    """
    if not db.text_index_present(conn):
        return []
    rows = conn.execute(
        """SELECT c.id, (SELECT text FROM doc_chunk_fts WHERE rowid=c.id) AS body
             FROM doc_chunks c WHERE c.file_id=? ORDER BY c.ordinal""",
        (file_id,),
    ).fetchall()
    return [(int(r["id"]), r["body"]) for r in rows if r["body"]]


def save_embeddings(conn: sqlite3.Connection, vectors: list[tuple[int, list[float]]]) -> None:
    """Write one document's vectors. Does not commit; the caller owns that."""
    for chunk_id, values in vectors:
        conn.execute(
            """INSERT INTO doc_chunk_embeddings(chunk_id, embedding, dimensions,
                                                embedder_version, embedded_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(chunk_id) DO UPDATE SET
                   embedding=excluded.embedding, dimensions=excluded.dimensions,
                   embedder_version=excluded.embedder_version,
                   embedded_at=excluded.embedded_at""",
            (
                chunk_id,
                struct.pack(f"<{len(values)}f", *values),
                len(values),
                MEANING_VERSION,
                db.now_iso(),
            ),
        )
