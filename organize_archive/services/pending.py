"""Pending-work counts: how much detection work is left to do.

The pipeline scheduler polls this (DB-only, no disk walk) to decide whether the
fused detect stage is startable. There was a count per detector back when
people and pets were separate stages; they are one pass over one decode now
(ADR 0004), so there is one backlog and one function that answers for it.
"""

from __future__ import annotations

import sqlite3

from ._common import _NOT_HIDDEN, _root_clause, reading

# int() around the fetched value in each function below is a no-op at runtime:
# a COUNT(*) always returns exactly one row holding an int. It is there
# because sqlite3.Row.__getitem__ is typed Any, which the `-> int` would
# otherwise silently swallow.


@reading
def detect_pending(
    conn: sqlite3.Connection,
    root_id: int | None = None,
    model_source: str | None = None,
    detect_video_frames: int = 0,
) -> int:
    """Present canonical media missing a current face OR pet scan — the fused
    detect stage's backlog (mirrors detect.extract.pending_count). One file is
    'pending' if either detector still owes it work, so the scheduler keeps the
    single detect stage running until both are drained.

    ``detect_video_frames`` mirrors ``cfg.detect_video_frames``: videos only
    count toward the backlog when it is > 0, otherwise the stage would never
    reach "up to date" while video detection is disabled."""
    rc, rp = _root_clause(root_id)
    media_types = "('image','video')" if detect_video_frames > 0 else "('image')"
    params = [model_source, *rp]
    row = conn.execute(
        f"""SELECT COUNT(*) FROM files f
            LEFT JOIN face_scan fs ON fs.file_id=f.id
            LEFT JOIN pet_scan ps ON ps.file_id=f.id
            WHERE (fs.file_id IS NULL
                   OR ps.file_id IS NULL
                   OR ps.source_sha256 IS NOT f.sha256
                   OR ps.model_source IS NOT ?)
              AND {_NOT_HIDDEN} AND f.media_type IN {media_types}{rc}""",
        params,
    ).fetchone()
    return int(row[0])
