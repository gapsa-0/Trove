"""Everything a user can change about People: edits, merges and constraints.

Split from ``services/people.py``, which is now the read side -- what the People
screens show. The seam is worth having because the two halves have almost
nothing in common: the reads are single queries shaped for a page, while
everything here has to keep four things consistent at once (a person's faces,
its stats, its centroid, and the durable pins that must survive the next
recluster) and is only correct as a set.

The original of the pattern ``services/pets_edit.py`` copies -- ``face_links``
is where the durable-link trick was invented and ``pet_links`` is the copy, so a
rule that looks odd here is probably load-bearing there too. People carry one
thing pets do not: ``persons.centroid``, kept current by
``_update_person_centroid`` after anything that moves faces.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, cast

from ..db import database as db
from . import edit_log, merging
from ._common import _QUALITY_OK, _quality_ok, writing

_PERSON = merging.LinkedSpec(
    entity=merging.EntitySpec(
        singular="person",
        plural="persons",
        table="persons",
        columns="id,name,cover_face_id,face_count",
    ),
    child_table="faces",
    fk="person_id",
    merge_table="person_merges",
    child_ids_column="face_ids",
    link_table="face_links",
    link_a="face_a",
    link_b="face_b",
)


# -- person bookkeeping ------------------------------------------------------
# The three things that must be re-derived whenever a face moves: the person's
# counts and cover, its centroid, and a stable id to anchor a durable link on.


def _rep_face(conn: sqlite3.Connection, pid: int, cover: int | None) -> int | None:
    """A stable representative face id for a person (its cover, or its sharpest
    face). Used to anchor a durable face_links constraint."""
    if cover:
        return cover
    r = conn.execute(
        f"SELECT id FROM faces WHERE person_id=? AND {_quality_ok('faces')} "
        f"ORDER BY det_score DESC LIMIT 1",
        (pid,),
    ).fetchone()
    return int(r["id"]) if r else None


def _sync_person_stats(conn: sqlite3.Connection, pid: int | None) -> None:
    """Recompute one person's face_count + cover after a face moves in/out; drop
    it if it's now empty. Mirrors faces/cluster.py's _refresh_person_stats."""
    if pid is None:
        return
    left = conn.execute(
        f"SELECT COUNT(*) FROM faces fa JOIN files f ON f.id=fa.file_id "
        f"WHERE fa.person_id=? AND f.hidden=0 AND {_QUALITY_OK}",
        (pid,),
    ).fetchone()[0]
    if left == 0 and not conn.execute("SELECT 1 FROM faces WHERE person_id=?", (pid,)).fetchone():
        conn.execute("DELETE FROM persons WHERE id=?", (pid,))
        return
    cover = conn.execute(
        f"SELECT fa.id FROM faces fa JOIN files f ON f.id=fa.file_id "
        f"WHERE fa.person_id=? AND f.hidden=0 AND {_QUALITY_OK} "
        f"ORDER BY fa.det_score DESC LIMIT 1",
        (pid,),
    ).fetchone()
    conn.execute(
        "UPDATE persons SET face_count=?, cover_face_id=? WHERE id=?",
        (left, cover["id"] if cover else None, pid),
    )


def _update_person_centroid(conn: sqlite3.Connection, pid: int) -> None:
    import numpy as np

    rows = conn.execute(
        f"SELECT fa.embedding e FROM faces fa JOIN files f ON f.id=fa.file_id "
        f"WHERE fa.person_id=? AND f.hidden=0 AND {_QUALITY_OK}",
        (pid,),
    ).fetchall()
    if not rows:
        return
    X = np.array([np.frombuffer(r["e"], "float32") for r in rows], dtype="float32")
    X /= np.linalg.norm(X, axis=1, keepdims=True) + 1e-9
    c = X.mean(0)
    c = (c / (np.linalg.norm(c) + 1e-9)).astype("float32")
    conn.execute("UPDATE persons SET centroid=? WHERE id=?", (c.tobytes(), pid))


# -- in-panel edits (face / person) ------------------------------------------


@writing
def rename_person(
    conn: sqlite3.Connection,
    person_id: int | None,
    name: str,
    pins: list[int] | None = None,
) -> dict[str, Any]:
    """Set a person's display name, keeping the manual face-pins in step.

    Pins are stored by NAME (``faces.manual_person``), which is what makes a
    user's "move this face to Mari" survive the DELETE/rebuild in
    ``faces/cluster.py``. So a rename has to move them and clearing a name has
    to RELEASE them: a pin left behind naming a person who no longer carries
    that name is not inert, it is an instruction, and ``_apply_manual_pins``
    obeys it by recreating the person on the next recluster -- under the name
    just removed, with a new id.

    ``pins`` re-pins the named faces, and exists for undo: the log records
    which pins a clearing released so putting the name back can put those back
    too. Restoring the name alone would scatter, at the next recluster, exactly
    the faces the name was placed on by hand.

    Returns ``{"error": ...}`` if ``person_id`` is missing or unknown.
    """
    if not person_id:
        return {"error": "missing person_id"}
    old = conn.execute("SELECT name FROM persons WHERE id=?", (person_id,)).fetchone()
    if not old:
        return {"error": "not found"}
    conn.execute("UPDATE persons SET name=? WHERE id=?", (name or None, person_id))
    released: list[int] = []
    if old["name"] and name and old["name"] != name:
        conn.execute("UPDATE faces SET manual_person=? WHERE manual_person=?", (name, old["name"]))
    elif old["name"] and not name:
        released = [
            int(r[0])
            for r in conn.execute("SELECT id FROM faces WHERE manual_person=?", (old["name"],))
        ]
        conn.execute("UPDATE faces SET manual_person=NULL WHERE manual_person=?", (old["name"],))
    if pins and name:
        marks = ",".join("?" for _ in pins)
        conn.execute(f"UPDATE faces SET manual_person=? WHERE id IN ({marks})", (name, *pins))
    # Recorded under the name it now has, so the entry is found again after a
    # recluster; `from` and `pins` are what undoing it puts back.
    edit_log.record(
        conn,
        edit_log.PERSON,
        person_id,
        name or old["name"],
        edit_log.RENAME,
        {"from": old["name"], "to": name or None, "pins": released},
    )
    conn.commit()
    return {"ok": True, "name": name or None}


@writing
def reassign_face(
    conn: sqlite3.Connection, face_id: int | None, person_id: int | None
) -> dict[str, Any]:
    """Move a face to a named person and PIN it (by name) so re-clustering keeps
    it there. Only named persons are valid targets."""
    if not face_id or not person_id:
        return {"error": "missing face_id or person_id"}
    fa = conn.execute("SELECT person_id FROM faces WHERE id=?", (face_id,)).fetchone()
    if not fa:
        return {"error": "unknown face"}
    p = conn.execute("SELECT id, name FROM persons WHERE id=?", (person_id,)).fetchone()
    if not p or not p["name"]:
        return {"error": "target must be a named person"}
    old_pid = fa["person_id"]
    conn.execute(
        "UPDATE faces SET person_id=?, manual_person=? WHERE id=?",
        (person_id, p["name"], face_id),
    )
    _sync_person_stats(conn, person_id)
    if old_pid and old_pid != person_id:
        _sync_person_stats(conn, old_pid)
    conn.commit()
    return {"ok": True, "person": {"id": p["id"], "name": p["name"]}}


@writing
def detach_file_from_person(
    conn: sqlite3.Connection, person_id: int | None, file_id: int | None
) -> dict[str, Any]:
    """ "This photo isn't them": release every face of this file currently
    assigned to person_id, and durably block them from drifting back.

    Each detached face is unassigned (person_id and manual_person cleared)
    and pinned with a cannot-link (face_links 'different') against the
    person's representative face, so a later recluster can't quietly re-add
    it -- same mechanism unmerge_persons uses, just against a single face
    instead of a whole cluster.

    Also drops any person_files manual tag for this (person, file) pair: a
    manual tag added earlier today would otherwise keep the photo showing on
    the person's page despite the detach (see person_files' schema comment)."""
    if not person_id or not file_id:
        return {"error": "missing person_id or file_id"}
    p = conn.execute("SELECT id, cover_face_id FROM persons WHERE id=?", (person_id,)).fetchone()
    if not p:
        return {"error": "unknown person"}
    faces = conn.execute(
        "SELECT id FROM faces WHERE file_id=? AND person_id=?", (file_id, person_id)
    ).fetchall()
    if not faces:
        return {"error": "this file has no face assigned to that person"}
    rep = _rep_face(conn, person_id, p["cover_face_id"])
    now = db.now_iso()
    face_ids = [r["id"] for r in faces]
    for fid in face_ids:
        merging.record_link(conn, _PERSON, rep, fid, "different", now)
    marks = ",".join("?" for _ in face_ids)
    conn.execute(
        f"UPDATE faces SET person_id=NULL, manual_person=NULL WHERE id IN ({marks})", face_ids
    )
    conn.execute("DELETE FROM person_files WHERE person_id=? AND file_id=?", (person_id, file_id))
    _sync_person_stats(conn, person_id)
    _update_person_centroid(conn, person_id)
    conn.commit()
    return {"ok": True, "detached_faces": len(face_ids)}


@writing
def add_person_to_file(
    conn: sqlite3.Connection, person_id: int | None, file_id: int | None
) -> dict[str, Any]:
    """Tag a file with a named person by hand, for media where no face was
    detected at all. Only named persons are valid targets (mirrors
    reassign_face) -- an unnamed auto-cluster id is ephemeral and wouldn't
    survive the next re-cluster anyway."""
    if not person_id or not file_id:
        return {"error": "missing person_id or file_id"}
    p = conn.execute("SELECT id, name FROM persons WHERE id=?", (person_id,)).fetchone()
    if not p or not p["name"]:
        return {"error": "target must be a named person"}
    if not conn.execute("SELECT 1 FROM files WHERE id=?", (file_id,)).fetchone():
        return {"error": "unknown file"}
    conn.execute(
        """INSERT OR REPLACE INTO person_files(person_id, file_id, person_name, created_at)
           VALUES(?,?,?,?)""",
        (person_id, file_id, p["name"], db.now_iso()),
    )
    edit_log.record(
        conn, edit_log.PERSON, person_id, p["name"], edit_log.ADD_PHOTO, {"file_id": file_id}
    )
    conn.commit()
    return {"ok": True, "person": {"id": p["id"], "name": p["name"]}}


@writing
def remove_person_from_file(
    conn: sqlite3.Connection, person_id: int | None, file_id: int | None
) -> dict[str, Any]:
    """Drop a manual person tag (person_files) from a file. Does not touch any
    detected face; returns ``{"error": ...}`` only when an id is missing, and
    ``{"ok": True}`` even if no such tag existed."""
    if not person_id or not file_id:
        return {"error": "missing person_id or file_id"}
    conn.execute("DELETE FROM person_files WHERE person_id=? AND file_id=?", (person_id, file_id))
    name = conn.execute("SELECT name FROM persons WHERE id=?", (person_id,)).fetchone()
    edit_log.record(
        conn,
        edit_log.PERSON,
        person_id,
        name["name"] if name else None,
        edit_log.REMOVE_PHOTO,
        {"file_id": file_id},
    )
    conn.commit()
    return {"ok": True}


@writing
def hide_person(
    conn: sqlite3.Connection, person_id: int | None, kind: str = "false_detection"
) -> dict[str, Any]:
    """User marked a cluster as NOT a person (a doll / animal / cartoon face that
    YuNet detected). Flag its faces so they're excluded from every future cluster,
    then drop the person. Durable and reversible only by clearing not_person."""
    if not person_id:
        return {"error": "missing person_id"}
    if not conn.execute("SELECT 1 FROM persons WHERE id=?", (person_id,)).fetchone():
        return {"error": "unknown person"}
    allowed = {"animal", "toy", "cartoon", "false_detection"}
    kind = kind if kind in allowed else "false_detection"
    conn.execute(
        """UPDATE faces SET not_person=1,person_id=NULL,
                            nonhuman_kind=?,nonhuman_source='manual'
           WHERE person_id=?""",
        (kind, person_id),
    )
    conn.execute("DELETE FROM persons WHERE id=?", (person_id,))
    conn.commit()
    return {"ok": True}


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
