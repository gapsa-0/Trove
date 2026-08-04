"""Pending-work counts: how much detection work is left to do.

The pipeline scheduler polls this (DB-only, no disk walk) to decide whether the
fused detect stage is startable. There was a count per detector back when
people and pets were separate stages; they are one pass over one decode now
(ADR 0004), so there is one backlog and one function that answers for it.
"""

from __future__ import annotations

import sqlite3

from ..detect.results import BOTH_DETECTORS, FACE, PET
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
    detectors: frozenset[str] = BOTH_DETECTORS,
) -> int:
    """Present canonical media a wanted detector still owes a scan — the fused
    detect stage's backlog (mirrors detect.extract.pending_count). One file is
    'pending' if any wanted detector still owes it work, so the scheduler keeps
    the single detect stage running until all of them are drained.

    ``detect_video_frames`` mirrors ``cfg.detect_video_frames``: videos only
    count toward the backlog when it is > 0, otherwise the stage would never
    reach "up to date" while video detection is disabled.

    ``detectors`` is the archive's People/Pets choice, translated to detector
    names by the caller. Asking about a detector the archive does not run would
    leave the stage permanently behind on work nobody wants done."""
    rc, rp = _root_clause(root_id)
    media_types = "('image','video')" if detect_video_frames > 0 else "('image')"
    owed, params = [], []
    if FACE in detectors:
        owed.append("fs.file_id IS NULL")
    if PET in detectors:
        owed.append(
            "ps.file_id IS NULL OR ps.source_sha256 IS NOT f.sha256 OR ps.model_source IS NOT ?"
        )
        params.append(model_source)
    if not owed:
        return 0
    row = conn.execute(
        f"""SELECT COUNT(*) FROM files f
            LEFT JOIN face_scan fs ON fs.file_id=f.id
            LEFT JOIN pet_scan ps ON ps.file_id=f.id
            WHERE ({" OR ".join(owed)})
              AND {_NOT_HIDDEN} AND f.media_type IN {media_types}{rc}""",
        [*params, *rp],
    ).fetchone()
    return int(row[0])
