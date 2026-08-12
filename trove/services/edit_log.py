"""What the user changed, and how to take it back.

The People and Pets screens are places you *work*: you name a cluster, fold two
together, drop a photo that isn't them. Until now the only one of those with any
memory was merging, and it announced itself as a panel wedged between a person's
name and their photographs -- the record of what you did sitting on top of the
thing you did it to.

This module is the memory for all of it. One row per edit, in the order they
happened, read back per person or per pet.

**It is not the undo.** The domain tables above it (``person_merges``,
``person_files``, ``face_links``) already hold what an undo actually needs, and
they are the ones clustering reads; a second copy of that would be a second
thing to keep true. A row here says what happened and points at the domain
record that owns the reversal, so ``undo`` below is a dispatcher, not a
mechanism.

Two consequences worth stating, because both look like bugs otherwise:

* **A row is marked undone, never deleted.** Undoing a merge deletes its
  ``person_merges`` row (``merging.unmerge_linked``), which is right -- that row
  is the instruction for a reversal that has now happened. But a history that
  erased itself as you used it would be a poor account of the afternoon, so the
  entry stays with ``undone_at`` set.
* **Reads fall back to the name.** ``cluster_faces`` DELETEs and rebuilds every
  ``persons`` row, so ``entity_id`` rots on the next detect chunk. The name is
  the durable anchor, exactly as in ``people._person_merges_for`` and
  ``manual_tags.repair_manual_person_files`` (ADR 0008). An unnamed cluster
  loses its history to a recluster, which was already true of its merges.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..db import database as db
from ._common import reading, writing

PERSON, PET = "person", "pet"

# Every action a row may carry. Named here rather than left implicit because the
# frontend words each one and `undo` dispatches on it, so a typo would surface
# as a silently unreadable, un-undoable row.
MERGE = "merge"
RENAME = "rename"
HIDE = "hide"
SET_COVER = "set_cover"
ADD_PHOTO = "add_photo"
REMOVE_PHOTO = "remove_photo"


def record(
    conn: sqlite3.Connection,
    entity: str,
    entity_id: int | None,
    entity_name: str | None,
    action: str,
    detail: dict[str, Any] | None = None,
    ref_table: str | None = None,
    ref_id: int | None = None,
) -> int:
    """Write one history entry. Takes a connection, not a path: every caller is
    already inside a ``@writing`` mutation and must land in the same transaction
    as the change it describes, or a crash between the two would leave a history
    that disagrees with the archive."""
    cur = conn.execute(
        """INSERT INTO edit_log(entity, entity_id, entity_name, action, detail,
                                ref_table, ref_id, created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (
            entity,
            entity_id,
            (entity_name or None),
            action,
            json.dumps(detail or {}),
            ref_table,
            ref_id,
            db.now_iso(),
        ),
    )
    return int(cur.lastrowid or 0)


def mark_undone(conn: sqlite3.Connection, ref_table: str, ref_id: int) -> None:
    """Mark whatever entry owns this domain row as undone.

    Called from the domain undo itself (``unmerge_persons``), so that undoing a
    merge from anywhere -- the history popover, or the Places-style panel that
    still exists elsewhere -- lands in the history either way.
    """
    conn.execute(
        "UPDATE edit_log SET undone_at=? WHERE ref_table=? AND ref_id=? AND undone_at IS NULL",
        (db.now_iso(), ref_table, ref_id),
    )


@reading
def entries_for(
    conn: sqlite3.Connection,
    entity: str,
    entity_id: int,
    name: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """This person's or pet's recent edits, newest first.

    Matched on id OR name for the reason in the module docstring. The name half
    is guarded on non-empty so that every unnamed cluster does not inherit every
    other unnamed cluster's history.
    """
    name = name or ""
    rows = conn.execute(
        """SELECT id, action, detail, ref_table, ref_id, created_at, undone_at
             FROM edit_log
            WHERE entity=? AND (entity_id=? OR (? != '' AND entity_name=?))
            ORDER BY created_at DESC, id DESC LIMIT ?""",
        (entity, entity_id, name, name, limit),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "action": r["action"],
            "detail": json.loads(r["detail"] or "{}"),
            "created_at": r["created_at"],
            "undone": r["undone_at"] is not None,
            # Only an entry with a domain row behind it can be reversed. A
            # rename carries its old name in `detail` and is reversible too;
            # anything else is a note, and the popover offers no button.
            "undoable": r["undone_at"] is None
            and (r["ref_table"] is not None or r["action"] == RENAME),
        }
        for r in rows
    ]


@reading
def _entry(conn: sqlite3.Connection, entry_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM edit_log WHERE id=?", (entry_id,)).fetchone()
    return dict(row) if row else None


@writing
def _close(conn: sqlite3.Connection, entry_id: int) -> dict[str, Any]:
    conn.execute("UPDATE edit_log SET undone_at=? WHERE id=?", (db.now_iso(), entry_id))
    conn.commit()
    return {"ok": True}


def undo(db_path: str, entry_id: int | None) -> dict[str, Any]:
    """Reverse one entry by handing off to whatever owns its reversal.

    Not decorated, and it calls the *public* service functions rather than
    reaching inside them: each of those manages its own connection and knows
    the four things a person has to keep consistent. This function's whole job
    is choosing which one to call.

    ``people_edit`` imports this module, so the dispatch imports it back inside
    the body. The cycle is real, and the repo breaks it this way elsewhere
    (``people.face_summary`` imports ``faces.backend`` in its own body).
    """
    from . import people_edit, pets_edit

    if not entry_id:
        return {"error": "missing entry_id"}
    row = _entry(db_path, entry_id)
    if row is None:
        return {"error": "unknown entry"}
    if row["undone_at"] is not None:
        return {"error": "already undone"}
    detail = json.loads(row["detail"] or "{}")
    is_pet = row["entity"] == PET

    if row["action"] == MERGE and row["ref_id"]:
        # The domain undo marks this entry itself, through mark_undone, so that
        # a merge undone from anywhere lands in the history the same way.
        unmerge = pets_edit.unmerge_pets if is_pet else people_edit.unmerge_persons
        return dict(unmerge(db_path, row["ref_id"]))
    if row["action"] == RENAME:
        result = (
            dict(pets_edit.rename_pet(db_path, row["entity_id"], detail.get("from") or ""))
            if is_pet
            # `pins` are the manual pins that clearing the name released. Put
            # the name back without them and the next recluster scatters the
            # faces it was placed on by hand.
            else dict(
                people_edit.rename_person(
                    db_path, row["entity_id"], detail.get("from") or "", detail.get("pins")
                )
            )
        )
        if not result.get("error"):
            # Two rows now describe one name: this one, marked undone, and the
            # rename that just reversed it. That is what happened, and it keeps
            # the Undo button off an entry that has already been used.
            _close(db_path, entry_id)
        return result
    return {"error": "this change cannot be undone"}
