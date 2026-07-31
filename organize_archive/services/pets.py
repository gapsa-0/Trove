"""Pets: non-human detections, pet identities, and the "same pet?" merge/undo.

Mirrors faces/people end to end -- `pets/cluster.py:cluster_pets` plays the
same role as `faces/cluster.py:cluster_faces`, `pet_links` the same role as
`face_links`, and `pet_merges` the same role as `person_merges`. Where a
function has a person-side counterpart that matters, its docstring names it.

That symmetry is not free: it is three near-identical merge transactions
across pets, people and places, which is what services/merging.py exists to
collapse.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path

from ..db import database as db
from ._common import _NOT_HIDDEN, _root_clause, reading, writing

# -- pets / non-human detections -------------------------------------------


@reading
def pet_summary(conn, root_id=None, model_source=None, detect_video_frames: int = 0) -> dict:
    from ..pets import backend as pet_backend

    rc, rp = _root_clause(root_id)
    # See face_summary: videos only join the counted population once the
    # detect stage actually processes them.
    media_types = "('image','video')" if detect_video_frames > 0 else "('image')"
    total = conn.execute(
        f"""SELECT COUNT(*) FROM files f WHERE {_NOT_HIDDEN}
            AND f.media_type IN {media_types}{rc}""",
        rp,
    ).fetchone()[0]
    scan_filter = " AND s.source_sha256 IS f.sha256"
    scan_params = list(rp)
    if model_source is not None:
        scan_filter += " AND s.model_source IS ?"
        scan_params.append(model_source)
    scanned = conn.execute(
        f"""SELECT COUNT(*) FROM pet_scan s JOIN files f ON f.id=s.file_id
            WHERE {_NOT_HIDDEN}{rc}{scan_filter}""",
        scan_params,
    ).fetchone()[0]
    detections = conn.execute(
        f"""SELECT COUNT(*) FROM animal_detections a
            JOIN files f ON f.id=a.file_id
            WHERE {_NOT_HIDDEN} AND a.species!='teddy bear'{rc}""",
        rp,
    ).fetchone()[0]
    groups = conn.execute(
        f"""SELECT COUNT(DISTINCT a.pet_id) FROM animal_detections a
            JOIN files f ON f.id=a.file_id
            WHERE {_NOT_HIDDEN} AND a.pet_id IS NOT NULL{rc}""",
        rp,
    ).fetchone()[0]
    nonhuman = conn.execute(
        f"""SELECT COUNT(*) FROM nonhuman_detections n
            JOIN files f ON f.id=n.file_id WHERE {_NOT_HIDDEN}{rc}""",
        rp,
    ).fetchone()[0]
    return {
        "total_images": total,
        "scanned": scanned,
        "unscanned": max(0, total - scanned),
        "detections": detections,
        "pets": groups,
        "nonhuman_faces": nonhuman,
        "backend_available": pet_backend.available(),
    }


@reading
def pet_groups(conn, root_id=None, limit=120, offset=0) -> dict:
    rc, rp = _root_clause(root_id)
    rows = conn.execute(
        f"""SELECT a.pet_id,p.name,p.species,p.cover_detection_id,
                   COUNT(*) detections,COUNT(DISTINCT a.file_id) photos
            FROM animal_detections a JOIN pets p ON p.id=a.pet_id
            JOIN files f ON f.id=a.file_id
            WHERE {_NOT_HIDDEN}{rc}
            GROUP BY a.pet_id
            ORDER BY CASE WHEN NULLIF(TRIM(p.name),'') IS NULL THEN 1 ELSE 0 END,
                     detections DESC,a.pet_id
            LIMIT ? OFFSET ?""",
        (*rp, limit, offset),
    ).fetchall()
    # Manual-only tags (pet_files), same treatment as face_persons: a
    # second small query scoped to this page's pet ids, counting only
    # files not already reachable through animal_detections for that pet.
    pids = [row["pet_id"] for row in rows]
    manual_counts: dict[int, int] = {}
    if pids:
        marks = ",".join("?" for _ in pids)
        rc2 = rc.replace("f.root_id", "f2.root_id") if rc else rc
        manual_rows = conn.execute(
            f"""SELECT pf.pet_id pid, COUNT(*) c
                FROM pet_files pf JOIN files f2 ON f2.id=pf.file_id
                WHERE pf.pet_id IN ({marks})
                  AND f2.present=1 AND f2.hidden=0{rc2}
                  AND pf.file_id NOT IN (
                      SELECT a2.file_id FROM animal_detections a2
                      WHERE a2.pet_id=pf.pet_id)
                GROUP BY pf.pet_id""",
            (*pids, *rp),
        ).fetchall()
        manual_counts = {row["pid"]: row["c"] for row in manual_rows}
    return {
        "pets": [
            {
                "id": row["pet_id"],
                "name": row["name"],
                "species": row["species"],
                "cover_detection_id": row["cover_detection_id"],
                "detections": row["detections"],
                "photos": row["photos"] + manual_counts.get(row["pet_id"], 0),
            }
            for row in rows
        ],
        "offset": offset,
        "count": len(rows),
    }


@reading
def animal_gallery(conn, root_id=None, limit=120, offset=0, unassigned=False) -> dict:
    rc, rp = _root_clause(root_id)
    un = " AND a.pet_id IS NULL" if unassigned else ""
    rows = conn.execute(
        f"""SELECT a.id detection_id,a.file_id,a.species,a.det_score,
                   f.rel_path,d.best_datetime dt
            FROM animal_detections a JOIN files f ON f.id=a.file_id
            LEFT JOIN dates d ON d.file_id=f.id
            WHERE {_NOT_HIDDEN} AND a.species!='teddy bear'{un}{rc}
            ORDER BY a.det_score DESC,a.id
            LIMIT ? OFFSET ?""",
        (*rp, limit, offset),
    ).fetchall()
    return {
        "items": [
            {
                "detection_id": row["detection_id"],
                "id": row["file_id"],
                "species": row["species"],
                "score": row["det_score"],
                "name": os.path.basename(row["rel_path"]),
                "date": row["dt"],
            }
            for row in rows
        ],
        "offset": offset,
        "count": len(rows),
    }


@reading
def pet_group(conn, pet_id: int, root_id=None, limit=120, offset=0):
    """Files this pet appears in: files with a detection of them, UNION ALL
    files manually tagged with them (pet_files) that don't already have such
    a detection -- mirrors face_person. Manual-only items carry
    detection_id=None."""
    pet = conn.execute("SELECT id,name,species FROM pets WHERE id=?", (pet_id,)).fetchone()
    if not pet:
        return None
    rc, rp = _root_clause(root_id)
    rows = conn.execute(
        f"""SELECT id, rel_path, dt, detection_id FROM (
                SELECT f.id AS id, f.rel_path, d.best_datetime AS dt,
                       a.id AS detection_id
                FROM animal_detections a JOIN files f ON f.id=a.file_id
                LEFT JOIN dates d ON d.file_id=f.id
                WHERE a.pet_id=? AND {_NOT_HIDDEN}{rc}
                UNION ALL
                SELECT f.id AS id, f.rel_path, d.best_datetime AS dt,
                       NULL AS detection_id
                FROM pet_files pf JOIN files f ON f.id=pf.file_id
                LEFT JOIN dates d ON d.file_id=f.id
                WHERE pf.pet_id=? AND {_NOT_HIDDEN}{rc}
                  AND pf.file_id NOT IN (
                      SELECT a2.file_id FROM animal_detections a2
                      WHERE a2.pet_id=?)
            )
            ORDER BY (dt IS NULL),dt DESC,id
            LIMIT ? OFFSET ?""",
        (pet_id, *rp, pet_id, *rp, pet_id, limit, offset),
    ).fetchall()
    total = conn.execute(
        f"""SELECT
                (SELECT COUNT(DISTINCT a.file_id) FROM animal_detections a
                 JOIN files f ON f.id=a.file_id
                 WHERE a.pet_id=? AND {_NOT_HIDDEN}{rc})
              + (SELECT COUNT(*) FROM pet_files pf
                 JOIN files f ON f.id=pf.file_id
                 WHERE pf.pet_id=? AND {_NOT_HIDDEN}{rc}
                   AND pf.file_id NOT IN (
                       SELECT a2.file_id FROM animal_detections a2
                       WHERE a2.pet_id=?))""",
        (pet_id, *rp, pet_id, *rp, pet_id),
    ).fetchone()[0]
    return {
        "id": pet["id"],
        "name": pet["name"],
        "species": pet["species"],
        "photos": total,
        "items": [
            {
                "id": row["id"],
                "name": os.path.basename(row["rel_path"]),
                "date": row["dt"],
                "detection_id": row["detection_id"],
                "type": "image",
                "has_gps": False,
            }
            for row in rows
        ],
        "offset": offset,
        "count": len(rows),
        "merges": _pet_merges_for(conn, pet_id, pet["name"]),
    }


def _pet_merges_for(conn, pet_id, name) -> list[dict]:
    """Merges this pet can undo. Looked up by survivor_id OR by survivor_name
    (when non-empty), mirroring _person_merges_for: cluster_pets rebuilds
    `pets` wholesale after every detect chunk, so a merge's survivor_id can
    rot -- the name is the durable anchor."""
    name = name or ""
    rows = conn.execute(
        """SELECT id, dropped_name, det_ids, created_at FROM pet_merges
           WHERE survivor_id=? OR (? != '' AND survivor_name=?)
           ORDER BY created_at DESC""",
        (pet_id, name, name),
    ).fetchall()
    out = []
    for row in rows:
        dids = json.loads(row["det_ids"])
        photos = 0
        if dids:
            marks = ",".join("?" for _ in dids)
            photos = conn.execute(
                f"SELECT COUNT(DISTINCT file_id) FROM animal_detections WHERE id IN ({marks})", dids
            ).fetchone()[0]
        out.append(
            {
                "id": row["id"],
                "dropped_name": row["dropped_name"],
                "photos_folded_in": photos,
                "created_at": row["created_at"],
            }
        )
    return out


@writing
def rename_pet(conn, pet_id, name: str) -> dict:
    if not conn.execute("SELECT 1 FROM pets WHERE id=?", (pet_id,)).fetchone():
        return {"error": "unknown pet"}
    conn.execute("UPDATE pets SET name=? WHERE id=?", (name or None, pet_id))
    conn.commit()
    return {"ok": True, "name": name or None}


@reading
def nonhuman_review(conn, root_id=None, limit=120, offset=0) -> dict:
    rc, rp = _root_clause(root_id)
    rows = conn.execute(
        f"""SELECT n.id,n.file_id,n.kind,n.confidence,n.source,n.review_status,
                   n.box_x,n.box_y,n.box_w,n.box_h,f.rel_path
            FROM nonhuman_detections n JOIN files f ON f.id=n.file_id
            WHERE {_NOT_HIDDEN}{rc}
            ORDER BY n.confidence DESC,n.id LIMIT ? OFFSET ?""",
        (*rp, limit, offset),
    ).fetchall()
    total = conn.execute(
        f"""SELECT COUNT(*) FROM nonhuman_detections n
            JOIN files f ON f.id=n.file_id WHERE {_NOT_HIDDEN}{rc}""",
        rp,
    ).fetchone()[0]
    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "offset": offset,
        "count": len(rows),
    }


def _bump_face_scan_after_restore(conn, file_id) -> None:
    """Shared face_scan counter fix for both "verdict=human" paths below."""
    conn.execute(
        """UPDATE face_scan SET n_faces=n_faces+1,
               rejected_nonhuman=MAX(0,rejected_nonhuman-1)
           WHERE file_id=?""",
        (file_id,),
    )


def _restore_existing_face(conn, detection_id, row) -> dict:
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


def _insert_face_from_detection(conn, row):
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


def _create_face_from_detection(conn, detection_id, row) -> dict:
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
def review_nonhuman(conn, detection_id, verdict: str) -> dict:
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
    if row["restored_face_id"]:
        return _restore_existing_face(conn, detection_id, row)
    return _create_face_from_detection(conn, detection_id, row)


@writing
def add_pet_to_file(conn, pet_id, file_id) -> dict:
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
def remove_pet_from_file(conn, pet_id, file_id) -> dict:
    if not pet_id or not file_id:
        return {"error": "missing pet_id or file_id"}
    conn.execute("DELETE FROM pet_files WHERE pet_id=? AND file_id=?", (pet_id, file_id))
    conn.commit()
    return {"ok": True}


def _rep_detection(conn, pid, cover) -> int | None:
    """A stable representative animal_detection id for a pet (its cover, or
    its highest-scoring detection). Mirrors _rep_face; used to anchor a
    durable pet_links constraint, which -- unlike a pet id -- survives
    cluster_pets' per-chunk DELETE/rebuild of the `pets` table."""
    if cover:
        return cover
    r = conn.execute(
        "SELECT id FROM animal_detections WHERE pet_id=? ORDER BY det_score DESC LIMIT 1", (pid,)
    ).fetchone()
    return r["id"] if r else None


# -- "same pet?" merges (durable) -------------------------------------------
#
# pets/cluster.py:cluster_pets DELETEs and rebuilds the whole `pets` table
# after every detect chunk, so a naive merge (just moving detections between
# pet ids) would be undone within a minute. `pet_links`, anchored to
# animal_detections ids rather than pet ids, is the constraint that survives
# that rebuild -- see pets/cluster.py's `_apply_links` for the clustering
# side of this, and faces/cluster.py's `_apply_links` / `face_links` for the
# original version of the same trick.


def _record_pet_link(conn, det_a, det_b, now: str) -> tuple[int, int] | None:
    """Write a durable 'same' pet_links constraint. Returns the (sorted) pair
    it wrote, or None if there was nothing to link -- mirrors _record_link;
    used by merge_pets to fill in pet_merges.link_a/link_b for undo."""
    if not det_a or not det_b or det_a == det_b:
        return None
    a, b = sorted((det_a, det_b))
    conn.execute(
        "INSERT OR REPLACE INTO pet_links(det_a, det_b, kind, created_at) VALUES(?,?,'same',?)",
        (a, b, now),
    )
    return (a, b)


def _refresh_pet_stats(conn, pet_id, name) -> None:
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
def merge_pets(conn, id_a, id_b, name=None) -> dict:
    """User confirmed two pet clusters are the same animal. Merge immediately
    (move detections, keep the named/larger one) AND store a durable 'same'
    pet_links constraint so the merge survives the next cluster_pets rebuild.
    Mirrors merge_persons; see its docstring for the explicit-`name` override."""
    if not id_a or not id_b or id_a == id_b:
        return {"error": "need two distinct pets"}
    pa = conn.execute(
        "SELECT id,name,cover_detection_id,detection_count FROM pets WHERE id=?", (id_a,)
    ).fetchone()
    pb = conn.execute(
        "SELECT id,name,cover_detection_id,detection_count FROM pets WHERE id=?", (id_b,)
    ).fetchone()
    if not pa or not pb:
        return {"error": "unknown pet"}
    name = (name or "").strip() or None
    if pa["name"] and pb["name"] and pa["name"] != pb["name"] and not name:
        return {"error": f"both named ({pa['name']} / {pb['name']}); choose a name"}
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
    now = db.now_iso()
    # Captured before the UPDATE below moves them, mirroring merge_persons
    # -- unmerge_pets needs to know which detections this merge folded in.
    drop_det_ids = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM animal_detections WHERE pet_id=?", (drop["id"],)
        ).fetchall()
    ]
    link = _record_pet_link(
        conn,
        _rep_detection(conn, keep["id"], keep["cover_detection_id"]),
        _rep_detection(conn, drop["id"], drop["cover_detection_id"]),
        now,
    )
    # foreign_keys=ON (see db.connect): animal_detections.pet_id is
    # ON DELETE SET NULL, so the UPDATE moving drop's detections MUST run
    # before DELETE FROM pets, or they'd be orphaned instead of merged.
    conn.execute("UPDATE animal_detections SET pet_id=? WHERE pet_id=?", (keep["id"], drop["id"]))
    conn.execute("DELETE FROM pets WHERE id=?", (drop["id"],))
    survivor_name = name or keep["name"] or drop["name"]
    _refresh_pet_stats(conn, keep["id"], survivor_name)
    # Recorded so this merge can be undone later (see unmerge_pets).
    conn.execute(
        """INSERT INTO pet_merges(survivor_id, survivor_name, dropped_name,
                                   det_ids, link_a, link_b, created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (
            keep["id"],
            survivor_name,
            drop["name"],
            json.dumps(drop_det_ids),
            link[0] if link else None,
            link[1] if link else None,
            now,
        ),
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
def unmerge_pets(conn, merge_id) -> dict:
    """Undo a drag-merge recorded by merge_pets. Mirrors unmerge_persons: the
    recorded 'same' pet_links row is deleted and replaced with a 'different'
    (cannot-link) row over the same detection pair, so cluster_pets' next
    rebuild can't silently re-form the merge (pets/cluster.py's _apply_links
    now honours 'different' the same way faces/cluster.py's does).

    Simpler than the person version: pet names aren't pinned per-detection
    (animal_detections.manual_pet exists in the schema but nothing writes to
    it yet), so there's no name pin to restore -- cluster_pets' own
    name-carryover (best overlap with a still-named pet) sorts the dropped
    name back out on the next rebuild by itself.

    Safe to call twice: a stale/missing merge_id returns a clean error rather
    than raising. Returns {"ok": True, "recluster": True} so the caller knows
    to kick off cluster_pets."""
    if not merge_id:
        return {"error": "missing merge_id"}
    m = conn.execute("SELECT * FROM pet_merges WHERE id=?", (merge_id,)).fetchone()
    if not m:
        return {"error": "merge not found"}
    now = db.now_iso()
    if m["link_a"] and m["link_b"]:
        conn.execute("DELETE FROM pet_links WHERE det_a=? AND det_b=?", (m["link_a"], m["link_b"]))
        conn.execute(
            "INSERT OR REPLACE INTO pet_links(det_a, det_b, kind, created_at) "
            "VALUES(?,?,'different',?)",
            (m["link_a"], m["link_b"], now),
        )
    conn.execute("DELETE FROM pet_merges WHERE id=?", (merge_id,))
    conn.commit()
    return {"ok": True, "recluster": True}


@reading
def animal_crop_source(conn, detection_id: int):
    """(abs path, sha256, box, rotate_deg, frame_offset, media_type, file_id)
    for an animal detection id. See ``face_crop_source`` for the video-frame
    caveats and what ``file_id`` is for."""
    row = conn.execute(
        """SELECT r.path root,f.rel_path,f.sha256,f.media_type,f.id AS file_id,
                  a.box_x,a.box_y,a.box_w,a.box_h,a.frame_offset,
                  COALESCE(o.rotate_deg, 0) AS rotate_deg
           FROM animal_detections a JOIN files f ON f.id=a.file_id
           JOIN roots r ON r.id=f.root_id
           LEFT JOIN orientation o ON o.file_id=f.id
           WHERE a.id=?""",
        (detection_id,),
    ).fetchone()
    if not row:
        return None
    path = Path(row["root"]) / row["rel_path"]
    if not path.is_file():
        return None
    return (
        path,
        row["sha256"],
        (row["box_x"], row["box_y"], row["box_w"], row["box_h"]),
        row["rotate_deg"],
        row["frame_offset"],
        row["media_type"],
        row["file_id"],
    )
