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
from typing import Any

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
        # A cover the user picked outranks the sharpest one we found; same
        # ordering as faces/cluster.py::_refresh_person_stats.
        f"ORDER BY fa.manual_cover DESC, fa.det_score DESC LIMIT 1",
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
    conn: sqlite3.Connection,
    person_id: int | None,
    kind: str = "false_detection",
    reason: str = "not_person",
) -> dict[str, Any]:
    """Take a cluster off the People screen, for one of two quite different reasons.

    ``not_person`` is a claim about the DETECTIONS: a doll, a statue, a face on
    a poster. Those are flagged ``not_person`` and leave clustering for good,
    and the persons row is deleted -- there was never a person there.

    ``unknown`` is a claim about the LIST: a real person you do not want on it.
    Their faces are untouched and go on clustering exactly as before, because
    they are faces of somebody; only the cluster's visibility changes. That is
    recorded in ``person_hides`` rather than only on the row, because the row
    does not survive the next recluster (see _reapply_person_hides).

    Collapsing the two would mean telling the clusterer that your neighbour is
    a doll, and there would be no way back from it.
    """
    if not person_id:
        return {"error": "missing person_id"}
    p = conn.execute(
        "SELECT id, name, cover_face_id FROM persons WHERE id=?", (person_id,)
    ).fetchone()
    if not p:
        return {"error": "unknown person"}
    if reason == "unknown":
        face_ids = [
            int(r[0]) for r in conn.execute("SELECT id FROM faces WHERE person_id=?", (person_id,))
        ]
        conn.execute("UPDATE persons SET hidden=1 WHERE id=?", (person_id,))
        conn.execute(
            """INSERT INTO person_hides(rep_face_id, person_name, face_ids, created_at)
               VALUES(?,?,?,?)""",
            (
                _rep_face(conn, person_id, p["cover_face_id"]),
                p["name"],
                json.dumps(face_ids),
                db.now_iso(),
            ),
        )
        edit_log.record(
            conn, edit_log.PERSON, person_id, p["name"], edit_log.HIDE, {"reason": "unknown"}
        )
        conn.commit()
        return {"ok": True}
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


@writing
def set_person_cover(
    conn: sqlite3.Connection, person_id: int | None, face_id: int | None
) -> dict[str, Any]:
    """Choose which face represents a person on their card.

    The pin goes on the FACE (``faces.manual_cover``), not just on
    ``persons.cover_face_id``, because the persons row is deleted and rebuilt by
    every recluster -- a choice recorded only there would hold until the next
    detect chunk and then quietly revert to the sharpest face. Every place that
    picks a cover ranks on this column first.
    """
    if not person_id or not face_id:
        return {"error": "missing person_id or face_id"}
    fa = conn.execute(
        f"SELECT id FROM faces WHERE id=? AND person_id=? AND {_quality_ok('faces')}",
        (face_id, person_id),
    ).fetchone()
    if not fa:
        # Either it is not theirs, or the quality gate hides it -- a cover
        # nobody can see is not a cover.
        return {"error": "that face is not one of this person's"}
    previous = conn.execute("SELECT cover_face_id FROM persons WHERE id=?", (person_id,)).fetchone()
    conn.execute("UPDATE faces SET manual_cover=0 WHERE person_id=?", (person_id,))
    conn.execute("UPDATE faces SET manual_cover=1 WHERE id=?", (face_id,))
    conn.execute("UPDATE persons SET cover_face_id=? WHERE id=?", (face_id, person_id))
    name = conn.execute("SELECT name FROM persons WHERE id=?", (person_id,)).fetchone()
    edit_log.record(
        conn,
        edit_log.PERSON,
        person_id,
        name["name"] if name else None,
        edit_log.SET_COVER,
        {"face_id": face_id, "from": previous["cover_face_id"] if previous else None},
    )
    conn.commit()
    return {"ok": True, "cover_face_id": face_id}


@writing
def unhide_person(conn: sqlite3.Connection, person_id: int | None) -> dict[str, Any]:
    """Put a hidden cluster back on the People screen.

    Only the ``unknown`` kind of hiding is reversible this way, and that is the
    whole reason the two are kept apart: a ``not_person`` verdict deleted the
    persons row and took its faces out of clustering, so there is no cluster
    left to unhide -- those come back through the Pets screen's non-human
    review, which restores the faces and lets the next pass rebuild the group.
    """
    if not person_id:
        return {"error": "missing person_id"}
    if not conn.execute("SELECT 1 FROM persons WHERE id=?", (person_id,)).fetchone():
        return {"error": "unknown person"}
    conn.execute("UPDATE persons SET hidden=0 WHERE id=?", (person_id,))
    conn.execute(
        """DELETE FROM person_hides WHERE rep_face_id IN
           (SELECT id FROM faces WHERE person_id=?)""",
        (person_id,),
    )
    conn.commit()
    return {"ok": True}
