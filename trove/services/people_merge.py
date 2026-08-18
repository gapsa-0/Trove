"""Folding two person clusters into one, and the constraints that survive it.

Split from ``people_edit.py`` at the seam that module already drew for itself
-- everything below its "same person?" review heading is here, unchanged. The
two halves are read for different questions: what one edit does to one person,
against what a merge has to keep true across four tables and the next
re-cluster, and only the second needs merging.py in its head.

The bookkeeping both halves need -- which faces a person has, what its stats
and centroid are, and the face a durable link anchors to -- stays in
``people_edit.py`` and is imported from there, the way ``_common.py``'s
predicates are imported by everything that has to agree about them.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, cast

from ..db import database as db
from . import edit_log, merging
from ._common import writing
from .people_edit import (
    _PERSON,
    _rep_face,
    _sync_person_stats,
    _update_person_centroid,
)

# -- "same person?" review: merges + constraints ---------------------------


def _finish_person_merge(
    conn: sqlite3.Connection, keep: sqlite3.Row, survivor_name: str | None
) -> None:
    """The survivor bookkeeping merge_persons runs after moving drop's faces
    onto keep and deleting drop: give the survivor its name, then recount and
    recentre it."""
    if survivor_name:
        conn.execute("UPDATE persons SET name=? WHERE id=?", (survivor_name, keep["id"]))
        # Load-bearing, not cosmetic: faces/cluster.py's _apply_manual_pins
        # recreates a person from any face still pinned (by NAME) to a
        # name that person no longer carries. Without this, a face that
        # was pinned to the LOSING name (e.g. the drop side of an
        # explicit-name merge of two differently-named clusters) would
        # still hold that stale manual_person and silently resurrect the
        # just-merged-away person on the very next recluster.
        conn.execute(
            "UPDATE faces SET manual_person=? WHERE person_id=? "
            "AND manual_person IS NOT NULL AND manual_person != ''",
            (survivor_name, keep["id"]),
        )
    _sync_person_stats(conn, keep["id"])
    _update_person_centroid(conn, keep["id"])


@writing
def merge_persons(
    conn: sqlite3.Connection, id_a: int | None, id_b: int | None, name: str | None = None
) -> dict[str, Any]:
    """User confirmed two clusters are the same person. Merge immediately (move
    faces, keep the named/larger one) AND store a durable 'same' constraint so
    the merge survives future re-clusters.

    An explicit `name` picks the surviving NAME outright; which person id
    survives is unaffected (see the ranking below). This is what lets two
    already-but-differently-named clusters merge at all: normally that's
    refused because there's no automatic way to choose between them."""
    pa, pb, err = merging.load_sides(conn, _PERSON.entity, id_a, id_b)
    if err:
        return err
    # load_sides ties err's nullness to pa/pb's, but mypy unpacks the union
    # element-wise and loses that coupling; err is None here so both rows
    # are guaranteed present.
    pa = cast(sqlite3.Row, pa)
    pb = cast(sqlite3.Row, pb)
    name, err = merging.resolve_name(pa, pb, name)
    if err:
        return err
    # Survivor: the named one, else the larger cluster, else the LOWER id --
    # ranking on (count, -id) settles both in one comparison. See ADR 0013.
    if pa["name"] and not pb["name"]:
        keep, drop = pa, pb
    elif pb["name"] and not pa["name"]:
        keep, drop = pb, pa
    elif (pa["face_count"] or 0, -pa["id"]) > (pb["face_count"] or 0, -pb["id"]):
        keep, drop = pa, pb
    else:
        keep, drop = pb, pa
    survivor_name = name or keep["name"]
    # Counted before the merge moves them, which is the only moment the losing
    # side is still a separate set of photographs.
    folded_in = conn.execute(
        "SELECT COUNT(DISTINCT file_id) FROM faces WHERE person_id=?", (drop["id"],)
    ).fetchone()[0]
    merge_id = merging.merge_linked(
        conn,
        _PERSON,
        keep,
        drop,
        survivor_name=survivor_name,
        rep=lambda c, side: _rep_face(c, side["id"], side["cover_face_id"]),
        finish=_finish_person_merge,
        now=db.now_iso(),
    )
    edit_log.record(
        conn,
        edit_log.PERSON,
        keep["id"],
        survivor_name,
        edit_log.MERGE,
        {"dropped_name": drop["name"], "photos": int(folded_in)},
        "person_merges",
        merge_id,
    )
    conn.commit()
    r = conn.execute("SELECT id,name,face_count FROM persons WHERE id=?", (keep["id"],)).fetchone()
    return {
        "ok": True,
        "person": {"id": r["id"], "name": r["name"], "face_count": r["face_count"]},
    }


def _restore_person_pins(conn: sqlite3.Connection, m: sqlite3.Row) -> None:
    """The name-restoration half of unmerge_persons; see its docstring."""
    survivor_name = m["survivor_name"]
    dropped_name = m["dropped_name"] or None
    face_ids = json.loads(m["face_ids"])
    if face_ids and survivor_name:
        marks = ",".join("?" for _ in face_ids)
        conn.execute(
            f"UPDATE faces SET manual_person=? WHERE id IN ({marks}) AND manual_person=?",
            (dropped_name, *face_ids, survivor_name),
        )


@writing
def unmerge_persons(conn: sqlite3.Connection, merge_id: int | None) -> dict[str, Any]:
    """Undo a drag-merge recorded by merge_persons. See merging.unmerge_linked
    for the cannot-link mechanism this writes, its known limitation, and the
    "safe to call twice" / recluster-return contract.

    People-specific: this also restores names. Any recorded face whose
    manual_person currently equals the survivor's name is pinned back to the
    dropped name (or NULL if the dropped side was unnamed), or
    faces/cluster.py's _apply_manual_pins would keep re-pinning it onto the
    survivor forever. See _finish_person_merge's "Load-bearing, not cosmetic"
    comment -- this undoes exactly that."""
    # Before unmerge_linked, which deletes the person_merges row this points at
    # and commits. Marking rather than deleting is why the history survives
    # being used; see services/edit_log.py.
    if merge_id:
        edit_log.mark_undone(conn, "person_merges", merge_id)
    return merging.unmerge_linked(conn, _PERSON, merge_id, restore=_restore_person_pins)


@writing
def _persons_link(
    conn: sqlite3.Connection, id_a: int | None, id_b: int | None, kind: str
) -> dict[str, Any]:
    """Record a durable pairwise constraint between two clusters (by their
    representative faces). 'different' = cannot-link (blocks future auto-merge);
    'skip' = "reviewed, undecided" (just drops the pair from the queue so it stops
    coming back). Neither changes the current clustering.

    The id checks below resemble ``merging.load_sides`` and deliberately are not
    it: that helper validates a *merge*, and both of these sides survive. See
    its docstring for why the four shared lines are not worth the blur."""
    if not id_a or not id_b or id_a == id_b:
        return {"error": "need two distinct persons"}
    pa = conn.execute("SELECT id,cover_face_id FROM persons WHERE id=?", (id_a,)).fetchone()
    pb = conn.execute("SELECT id,cover_face_id FROM persons WHERE id=?", (id_b,)).fetchone()
    if not pa or not pb:
        return {"error": "unknown person"}
    merging.record_link(
        conn,
        _PERSON,
        _rep_face(conn, pa["id"], pa["cover_face_id"]),
        _rep_face(conn, pb["id"], pb["cover_face_id"]),
        kind,
        db.now_iso(),
    )
    conn.commit()
    return {"ok": True}


def set_persons_different(db_path: str, id_a: int | None, id_b: int | None) -> dict[str, Any]:
    """Record that two person clusters are NOT the same person: a durable
    cannot-link in face_links, which blocks a future automatic merge and drops
    the pair out of the person_suggestions queue. Returns ``{"error": ...}`` if
    the ids are missing, equal, or unknown."""
    return _persons_link(db_path, id_a, id_b, "different")


def set_persons_skip(db_path: str, id_a: int | None, id_b: int | None) -> dict[str, Any]:
    """Record that a "same person?" suggestion was reviewed and left undecided:
    the pair stops being offered by person_suggestions, but nothing is asserted
    about it either way. Returns ``{"error": ...}`` if the ids are missing,
    equal, or unknown."""
    return _persons_link(db_path, id_a, id_b, "skip")
