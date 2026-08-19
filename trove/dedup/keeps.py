"""Which copy of a duplicate group is the one Browse shows.

By default that is the group's canonical -- the biggest, best-provenanced copy
(``groups.pick_canonical``) -- and every other member is hidden. A user can
disagree, for reasons no ranking can know: the "worse" copy is the one already
in the album that gets shared, or two of the copies are not really the same
picture and both should stay.

So the choice is stored per FILE in ``dup_keeps`` and applied here. Both halves
matter:

* **Per file, not per group.** Grouping deletes and rebuilds `dup_groups` and
  `dup_members` on every run, so a choice recorded against a group id would last
  until the next batch of photos arrived. File ids survive, which is why
  `person_hides` anchors to a face and `pet_hides` to a detection.
* **Applied, not consulted.** `files.hidden` stays the single answer to "does
  Browse show this", so nothing downstream -- the grid, the counts, the People
  backlog -- has to learn about a second source of truth.
"""

from __future__ import annotations

import sqlite3

# A group under the user's control is one where at least one of its members has
# a keep row. Everything else follows the automatic rule.
_OVERRIDDEN = """SELECT DISTINCT m.group_id FROM dup_members m
                 JOIN dup_keeps k ON k.file_id = m.file_id"""


def apply(conn: sqlite3.Connection) -> None:
    """Set `files.hidden` for every grouped file, and recount what that saves.

    Written as three sweeps over the whole grouping rather than a loop per
    group: this runs at the end of every dedup pass, where the loop would be one
    statement per group on an archive that has tens of thousands of them.

    The order is load-bearing. Hiding every member of an overridden group first
    and then un-hiding the kept ones means a group whose kept files have all
    been deleted since would end up with nothing visible -- so the fallback
    sweep runs last and gives such a group its canonical back. That is the only
    way a group can end up with no keeps at all, since the service refuses to
    write an empty set.
    """
    # 1. The automatic rule, for every group nobody has overridden.
    conn.execute(
        f"""UPDATE files SET hidden = CASE
                WHEN id IN (SELECT canonical_file_id FROM dup_groups) THEN 0 ELSE 1 END
            WHERE dup_group_id IS NOT NULL AND dup_group_id NOT IN ({_OVERRIDDEN})"""
    )
    # 2. The user's, for the rest: exactly the members they kept.
    conn.execute(
        f"""UPDATE files SET hidden = CASE
                WHEN id IN (SELECT file_id FROM dup_keeps) THEN 0 ELSE 1 END
            WHERE dup_group_id IN ({_OVERRIDDEN})"""
    )
    # 3. A group left with nothing visible gets its canonical back. Reachable
    #    only through deletion -- the files that were kept are gone, and the
    #    keep rows went with them -- but a group that hides every copy of itself
    #    is a picture missing from Browse with nothing to say where it went.
    conn.execute(
        """UPDATE files SET hidden=0 WHERE id IN (
               SELECT g.canonical_file_id FROM dup_groups g
               WHERE NOT EXISTS (SELECT 1 FROM dup_members m JOIN files f ON f.id=m.file_id
                                 WHERE m.group_id=g.id AND f.hidden=0))"""
    )
    _recount(conn)


def _recount(conn: sqlite3.Connection) -> None:
    """Re-derive `redundant_bytes` from what is actually hidden.

    It is written at grouping time as "everything but the canonical", which
    stops being true the moment a second copy is kept: the screen would go on
    offering space back for a file it is showing you.
    """
    conn.execute(
        """UPDATE dup_groups SET redundant_bytes = COALESCE((
               SELECT SUM(f.size) FROM dup_members m JOIN files f ON f.id=m.file_id
               WHERE m.group_id = dup_groups.id AND f.hidden = 1), 0)"""
    )
