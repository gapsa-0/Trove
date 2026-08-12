"""Pets, change side: renames, the non-human review, manual tags and merges.

Split from ``services/pets.py`` (now the read side) along the same line as
``people_edit.py``, and for the same reason -- see that module's header. Where a
function has a person-side counterpart that matters, its docstring names it.

That symmetry is not free: it is three near-identical merge transactions across
pets, people and places, which is what services/merging.py exists to collapse.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from typing import Any, cast

from ..db import database as db
from . import edit_log, merging
from ._common import writing

# -- "same pet?" merges (durable) -------------------------------------------
#
# pets/cluster.py:cluster_pets DELETEs and rebuilds the whole `pets` table
# after every detect chunk, so a naive merge (just moving detections between
# pet ids) would be undone within a minute. `pet_links`, anchored to
# animal_detections ids rather than pet ids, is the constraint that survives
# that rebuild -- see pets/cluster.py's `_apply_links` for the clustering
# side of this, and faces/cluster.py's `_apply_links` / `face_links` for the
# original version of the same trick.

_PET = merging.LinkedSpec(
    entity=merging.EntitySpec(
        singular="pet",
        plural="pets",
        table="pets",
        columns="id,name,cover_detection_id,detection_count",
    ),
    child_table="animal_detections",
    fk="pet_id",
    merge_table="pet_merges",
    child_ids_column="det_ids",
    link_table="pet_links",
    link_a="det_a",
    link_b="det_b",
)


def _rep_detection(conn: sqlite3.Connection, pid: int, cover: int | None) -> int | None:
    """A stable representative animal_detection id for a pet (its cover, or
    its highest-scoring detection). Mirrors _rep_face; used to anchor a
    durable pet_links constraint, which -- unlike a pet id -- survives
    cluster_pets' per-chunk DELETE/rebuild of the `pets` table."""
    if cover:
        return cover
    r = conn.execute(
        "SELECT id FROM animal_detections WHERE pet_id=? ORDER BY det_score DESC LIMIT 1", (pid,)
    ).fetchone()
    return int(r["id"]) if r else None


def _refresh_pet_stats(conn: sqlite3.Connection, pet_id: int, name: str | None) -> None:
    """Recompute a pet's aggregate fields from its current detections and set
    its name. Used right after merge_pets moves detections into the surviving
    pet, so a merged pet ends up with the same shape cluster_pets would have
    produced had it clustered this combined group directly."""
    import numpy as np

    rows = conn.execute(
        "SELECT id, species, det_score, embedding FROM animal_detections WHERE pet_id=?", (pet_id,)
    ).fetchall()
    cover = max(rows, key=lambda r: r["det_score"]) if rows else None
    # Majority species among the merged detections, ties broken by the
    # best-scoring detection among the tied species -- the same rule
    # cluster_pets applies to a pet_links-merged group, so a merge and the
    # rebuild that follows it agree (a dog once misdetected as a cat merges in).
    species = None
    if rows:
        counts = Counter(r["species"] for r in rows)
        top = max(counts.values())
        tied = {sp for sp, c in counts.items() if c == top}
        species = max((r for r in rows if r["species"] in tied), key=lambda r: r["det_score"])[
            "species"
        ]
    emb_rows = [r for r in rows if r["embedding"]]
    centroid = None
    if emb_rows:
        V = np.array([np.frombuffer(r["embedding"], "float32") for r in emb_rows], dtype="float32")
        V /= np.linalg.norm(V, axis=1, keepdims=True) + 1e-9
        c = V.mean(axis=0)
        c = (c / (np.linalg.norm(c) + 1e-9)).astype("float32")
        centroid = c.tobytes()
    conn.execute(
        """UPDATE pets SET name=?, species=?, cover_detection_id=?,
                          detection_count=?, centroid=? WHERE id=?""",
        (name, species, cover["id"] if cover else None, len(rows), centroid, pet_id),
    )


@writing
def rename_pet(conn: sqlite3.Connection, pet_id: int | None, name: str) -> dict[str, Any]:
    """Set a pet's display name. Returns ``{"error": "unknown pet"}`` if
    ``pet_id`` doesn't exist."""
    old = conn.execute("SELECT name FROM pets WHERE id=?", (pet_id,)).fetchone()
    if not old:
        return {"error": "unknown pet"}
    conn.execute("UPDATE pets SET name=? WHERE id=?", (name or None, pet_id))
    edit_log.record(
        conn,
        edit_log.PET,
        pet_id,
        name or old["name"],
        edit_log.RENAME,
        {"from": old["name"], "to": name or None},
    )
    conn.commit()
    return {"ok": True, "name": name or None}


def _bump_face_scan_after_restore(conn: sqlite3.Connection, file_id: int) -> None:
    """Shared face_scan counter fix for both "verdict=human" paths below."""
    conn.execute(
        """UPDATE face_scan SET n_faces=n_faces+1,
               rejected_nonhuman=MAX(0,rejected_nonhuman-1)
           WHERE file_id=?""",
        (file_id,),
    )


def _restore_existing_face(
    conn: sqlite3.Connection, detection_id: int, row: sqlite3.Row
) -> dict[str, Any]:
    """verdict=human, and the detection already has a restored_face_id.

    Un-hide that existing face row, mark the detection human, fix the
    face_scan counters.
    """
    conn.execute(
        """UPDATE faces SET not_person=0,nonhuman_kind=NULL,
                            nonhuman_source=NULL
           WHERE id=?""",
        (row["restored_face_id"],),
    )
    conn.execute("UPDATE nonhuman_detections SET review_status='human' WHERE id=?", (detection_id,))
    _bump_face_scan_after_restore(conn, row["file_id"])
    conn.commit()
    return {
        "ok": True,
        "status": "human",
        "root_id": row["root_id"],
        "face_id": row["restored_face_id"],
    }


def _insert_face_from_detection(conn: sqlite3.Connection, row: sqlite3.Row) -> sqlite3.Cursor:
    """INSERT a new faces row from a nonhuman_detections row's own columns.

    Returns the cursor so the caller can read ``lastrowid``.
    """
    return conn.execute(
        """INSERT INTO faces
           (file_id,box_x,box_y,box_w,box_h,det_score,focus_score,brightness,
            extreme_fraction,clipped_fraction,quality_score,quality_source,
            embedding,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            row["file_id"],
            row["box_x"],
            row["box_y"],
            row["box_w"],
            row["box_h"],
            row["det_score"],
            row["focus_score"],
            row["brightness"],
            row["extreme_fraction"],
            row["clipped_fraction"],
            row["quality_score"],
            row["quality_source"],
            row["embedding"],
            db.now_iso(),
        ),
    )


def _create_face_from_detection(
    conn: sqlite3.Connection, detection_id: int, row: sqlite3.Row
) -> dict[str, Any]:
    """verdict=human, with no restored_face_id yet.

    Require a retained embedding, INSERT a new faces row from the
    detection's columns, point the detection at it, fix the face_scan
    counters.
    """
    if not row["embedding"]:
        return {"error": "candidate has no retained embedding; rescan is required"}
    cursor = _insert_face_from_detection(conn, row)
    conn.execute(
        """UPDATE nonhuman_detections
           SET review_status='human',restored_face_id=? WHERE id=?""",
        (cursor.lastrowid, detection_id),
    )
    _bump_face_scan_after_restore(conn, row["file_id"])
    conn.commit()
    return {
        "ok": True,
        "status": "human",
        "root_id": row["root_id"],
        "face_id": cursor.lastrowid,
    }


@writing
def review_nonhuman(
    conn: sqlite3.Connection, detection_id: int | None, verdict: str
) -> dict[str, Any]:
    """Confirm a non-human candidate or restore it to People as unassigned."""
    if verdict not in {"confirmed", "human"}:
        return {"error": "verdict must be confirmed or human"}
    row = conn.execute(
        """SELECT n.*,f.root_id FROM nonhuman_detections n
           JOIN files f ON f.id=n.file_id WHERE n.id=?""",
        (detection_id,),
    ).fetchone()
    if not row:
        return {"error": "unknown non-human detection"}
    if verdict == "confirmed":
        conn.execute(
            "UPDATE nonhuman_detections SET review_status='confirmed' WHERE id=?",
            (detection_id,),
        )
        conn.commit()
        return {"ok": True, "status": "confirmed", "root_id": row["root_id"]}
    # `row` was found by matching n.id=detection_id above, so detection_id
    # cannot have been None here -- narrow the type, not the value.
    detection_id = cast(int, detection_id)
    if row["restored_face_id"]:
        return _restore_existing_face(conn, detection_id, row)
    return _create_face_from_detection(conn, detection_id, row)


@writing
def add_pet_to_file(
    conn: sqlite3.Connection, pet_id: int | None, file_id: int | None
) -> dict[str, Any]:
    """Tag a file with a named pet by hand. Same shape as add_person_to_file,
    against pet_files/pets."""
    if not pet_id or not file_id:
        return {"error": "missing pet_id or file_id"}
    p = conn.execute("SELECT id, name FROM pets WHERE id=?", (pet_id,)).fetchone()
    if not p or not p["name"]:
        return {"error": "target must be a named pet"}
    if not conn.execute("SELECT 1 FROM files WHERE id=?", (file_id,)).fetchone():
        return {"error": "unknown file"}
    conn.execute(
        """INSERT OR REPLACE INTO pet_files(pet_id, file_id, pet_name, created_at)
           VALUES(?,?,?,?)""",
        (pet_id, file_id, p["name"], db.now_iso()),
    )
    conn.commit()
    return {"ok": True, "pet": {"id": p["id"], "name": p["name"]}}


@writing
def remove_pet_from_file(
    conn: sqlite3.Connection, pet_id: int | None, file_id: int | None
) -> dict[str, Any]:
    """Drop a manual pet tag (pet_files) from a file. Does not touch any
    detection; returns ``{"error": ...}`` only when an id is missing, and
    ``{"ok": True}`` even if no such tag existed."""
    if not pet_id or not file_id:
        return {"error": "missing pet_id or file_id"}
    conn.execute("DELETE FROM pet_files WHERE pet_id=? AND file_id=?", (pet_id, file_id))
    conn.commit()
    return {"ok": True}


@writing
def merge_pets(
    conn: sqlite3.Connection, id_a: int | None, id_b: int | None, name: str | None = None
) -> dict[str, Any]:
    """User confirmed two pet clusters are the same animal. Merge immediately
    (move detections, keep the named/larger one) AND store a durable 'same'
    pet_links constraint so the merge survives the next cluster_pets rebuild.
    Shares merge_persons' mechanics, not its rule; see it for `name`."""
    pa, pb, err = merging.load_sides(conn, _PET.entity, id_a, id_b)
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
    # Survivor: the named one, else the larger detection_count, else the
    # lower id (deterministic tiebreak so repeated merges are stable).
    if pa["name"] and not pb["name"]:
        keep, drop = pa, pb
    elif pb["name"] and not pa["name"]:
        keep, drop = pb, pa
    elif (pa["detection_count"] or 0) != (pb["detection_count"] or 0):
        keep, drop = (
            (pa, pb) if (pa["detection_count"] or 0) > (pb["detection_count"] or 0) else (pb, pa)
        )
    elif pa["id"] < pb["id"]:
        keep, drop = pa, pb
    else:
        keep, drop = pb, pa
    survivor_name = name or keep["name"] or drop["name"]
    folded_in = conn.execute(
        "SELECT COUNT(DISTINCT file_id) FROM animal_detections WHERE pet_id=?", (drop["id"],)
    ).fetchone()[0]
    merge_id = merging.merge_linked(
        conn,
        _PET,
        keep,
        drop,
        survivor_name=survivor_name,
        rep=lambda c, side: _rep_detection(c, side["id"], side["cover_detection_id"]),
        finish=lambda c, keep_row, chosen: _refresh_pet_stats(c, keep_row["id"], chosen),
        now=db.now_iso(),
    )
    edit_log.record(
        conn,
        edit_log.PET,
        keep["id"],
        survivor_name,
        edit_log.MERGE,
        {"dropped_name": drop["name"], "photos": int(folded_in)},
        "pet_merges",
        merge_id,
    )
    conn.commit()
    r = conn.execute(
        "SELECT id,name,species,detection_count FROM pets WHERE id=?", (keep["id"],)
    ).fetchone()
    return {
        "ok": True,
        "pet": {
            "id": r["id"],
            "name": r["name"],
            "species": r["species"],
            "detections": r["detection_count"],
        },
    }


@writing
def unmerge_pets(conn: sqlite3.Connection, merge_id: int | None) -> dict[str, Any]:
    """Undo a drag-merge recorded by merge_pets. See merging.unmerge_linked
    for the shared mechanics (the cannot-link write and the safe-to-call-twice
    contract).

    Simpler than the person version: pet names aren't pinned per-detection
    (animal_detections.manual_pet exists in the schema but nothing writes to
    it yet), so there's no name pin to restore -- cluster_pets' own
    name-carryover (best overlap with a still-named pet) sorts the dropped
    name back out on the next rebuild by itself."""
    if merge_id:
        edit_log.mark_undone(conn, "pet_merges", merge_id)
    return merging.unmerge_linked(conn, _PET, merge_id)
