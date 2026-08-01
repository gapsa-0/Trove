"""SQL fragments and helpers shared by more than one service module.

Nothing domain-specific belongs here: a predicate used by exactly one module
lives in that module. What is here is here because two or more domains must
agree on it, and the comments explain what breaks when they disagree.
"""

from __future__ import annotations

import functools
import sqlite3
from collections.abc import Callable
from typing import Concatenate

from ..db import database as db


def reading[**P, R](
    fn: Callable[Concatenate[sqlite3.Connection, P], R],
) -> Callable[Concatenate[str, P], R]:
    """Open a read-only connection, pass it to ``fn``, always close it.

    A connection per call looks wasteful next to reusing one, but it is the
    correct shape here: handlers run concurrently under ``ThreadingHTTPServer``,
    a sqlite3 connection is not safe to share between threads, and opening one
    against a local file is cheap -- there is no pool to warm. Sharing one
    instead would not be slow, it would be a crash (or silent corruption) the
    first time two requests land on it at once.

    The wrapped function is called as ``fn(conn, *args, **kwargs)``, but the
    decorated function keeps the public signature ``(db_path, *args,
    **kwargs)`` -- callers never see the connection, and nothing about how
    they invoke a service function changes.
    """

    @functools.wraps(fn)
    def wrapper(db_path: str, *args: P.args, **kwargs: P.kwargs) -> R:
        conn = db.open_readonly(db_path)
        try:
            return fn(conn, *args, **kwargs)
        finally:
            conn.close()

    return wrapper


def writing[**P, R](
    fn: Callable[Concatenate[sqlite3.Connection, P], R],
) -> Callable[Concatenate[str, P], R]:
    """Open a read-write connection, pass it to ``fn``, always close it.

    Same per-call-connection reasoning as ``@reading`` (see its docstring):
    thread safety under ``ThreadingHTTPServer``, not a performance concern.

    Two things this decorator deliberately does NOT do:

    - **It does not commit.** ``fn`` still calls ``conn.commit()`` itself.
      Where the transaction boundary falls -- one commit for the whole
      function, or several -- is that function's business rule, not
      something a generic wrapper should decide for it.
    - **It does not retry on a locked database.** ``db.write_with_retry`` is
      applied by the caller (``web/server.py`` wraps 21 mutation call sites
      in it), one layer up from here. Retrying inside this decorator would
      move where a lock gets handled without anyone deciding that on
      purpose, and would retry a whole request handler rather than the one
      write that actually needs it.
    """

    @functools.wraps(fn)
    def wrapper(db_path: str, *args: P.args, **kwargs: P.kwargs) -> R:
        conn = db.connect(db_path)
        try:
            return fn(conn, *args, **kwargs)
        finally:
            conn.close()

    return wrapper


# Analytics (summary, timeline, map, counts) describe the whole archive, so they
# count every present file. Only Browse hides non-canonical duplicates.
_VISIBLE = "f.present = 1"
_NOT_HIDDEN = "f.present = 1 AND f.hidden = 0"

# "This file has a location." One definition for both the 📍 tile badge and the
# "Show only files with a location" box, so the badge and the filter can never
# disagree about what counts. A file has a location iff it has a geo row: the
# resolver already declines to write 0/0/0 (Takeout's "no location"), so there
# are no null-island rows to exclude here.
_HAS_LOCATION = "EXISTS(SELECT 1 FROM geo g WHERE g.file_id=f.id)"


def _quality_ok(alias: str = "fa") -> str:
    """SQL predicate excluding faces the FIQA gate rejected (faces/fiqa.py).

    LOW_QUALITY faces are kept in the database — never deleted, so the call stays
    auditable and re-tierable — but they must not appear anywhere in the UI: not
    in a photo's detected faces, not in a person's photo list, not in any count.
    This predicate is that rule, in one place.

    NULL-safe on purpose: a face extracted before the gate existed has no tier,
    and an unknown quality must not make a face vanish. It reads as BORDERLINE,
    which is exactly how faces/cluster.py treats it too.
    """
    return f"COALESCE({alias}.quality_tier, 'BORDERLINE') != 'LOW_QUALITY'"


_QUALITY_OK = _quality_ok()


def _root_clause(root_id: int | None) -> tuple[str, list[int]]:
    if root_id is None:
        return "", []
    return " AND f.root_id = ?", [root_id]


def _indexed_exists(alias: str = "f") -> str:
    """SQL for "this file carries a *current* description-search vector".

    Deliberately the same three conditions semantic_summary counts as `indexed`,
    so the grid's markers and the reach note above it can never disagree:

    - ``status='indexed'`` — a skipped or failed file has a row too, and cannot
      answer a query,
    - ``source_sha256=f.sha256`` — the vector describes the bytes the file has
      *now*; if the content changed underneath it, it is stale,
    - ``indexer_version`` — a vector from a previous model does not live in the
      same space as today's query, so it is not findable even though it exists.

    That last one is why an archive mid-migration shows every tile unmarked
    rather than falsely marked: 69,726 Voyage-era rows are still on disk here.
    Takes one parameter, the current indexer version.
    """
    return (
        f"EXISTS(SELECT 1 FROM semantic_embeddings se "
        f"WHERE se.file_id={alias}.id AND se.source_sha256={alias}.sha256 "
        f"AND COALESCE(se.indexer_version,'')=? AND se.status='indexed')"
    )
