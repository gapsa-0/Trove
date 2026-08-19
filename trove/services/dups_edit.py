"""Choosing which copies of a duplicate group stay visible.

The read side is ``services/dups.py``; this is the one thing a user can change
about a group. Split along the same line as ``people.py`` / ``people_edit.py``,
and for the same reason: the reads are queries shaped for a screen, while this
has to keep two things true at once (which files Browse shows, and what the
screen offers to reclaim) and is only correct as a set.

What a keep MEANS, and where it is applied, is ``dedup/keeps.py``. This module
is the validation around it -- which is most of the work, because the one rule
that cannot be bent is that a group always shows at least one of its copies.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..db import database as db
from ..dedup import keeps
from ._common import writing


@writing
def set_kept_copies(
    conn: sqlite3.Connection, group_id: int | None, file_ids: list[int] | None
) -> dict[str, Any]:
    """Say which copies of a duplicate group Browse should show.

    ``file_ids`` is the whole kept set, not a change to it: the screen sends
    what it is showing, so two people editing the same group cannot combine
    into a set neither of them chose.

    Refuses an empty set outright. A group that shows none of its copies is a
    picture missing from Browse with nothing on any screen to say where it went
    -- the Duplicates page lists groups, not files, so it would not even be
    somewhere to look. The screen also prevents it (the last kept copy's toggle
    is disabled), and this is the same rule where it cannot be bypassed.

    Passing exactly the automatic answer -- the canonical alone -- clears the
    override rather than recording it, so a group the user has put back the way
    they found it goes on following the canonical if a later regroup picks a
    different one.
    """
    if not group_id or not file_ids:
        return {"error": "a group must keep at least one copy"}
    group = conn.execute(
        "SELECT id, canonical_file_id FROM dup_groups WHERE id=?", (group_id,)
    ).fetchone()
    if not group:
        return {"error": "unknown group"}
    members = {
        int(r[0])
        for r in conn.execute("SELECT file_id FROM dup_members WHERE group_id=?", (group_id,))
    }
    wanted = {int(f) for f in file_ids}
    if not wanted <= members:
        return {"error": "those files are not all copies in that group"}
    conn.execute(
        """DELETE FROM dup_keeps WHERE file_id IN
           (SELECT file_id FROM dup_members WHERE group_id=?)""",
        (group_id,),
    )
    if wanted != {group["canonical_file_id"]}:
        now = db.now_iso()
        conn.executemany(
            "INSERT INTO dup_keeps(file_id, created_at) VALUES(?,?)",
            [(file_id, now) for file_id in sorted(wanted)],
        )
    keeps.apply(conn)
    conn.commit()
    return {"ok": True, "kept": sorted(wanted)}
