"""People: faces, person identities, and the "same person?" review merge/undo.

The original of the pattern services/pets.py copies -- `face_links` is where
the durable-link trick was invented and `pet_links` is the copy, so a rule
that looks odd here is probably load-bearing there too. People carry one
thing pets do not: `persons.centroid`, kept current by
`_update_person_centroid` on every merge.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..db import database as db
from . import merging
from ._common import _NOT_HIDDEN, _QUALITY_OK, _quality_ok, _root_clause, reading, writing


@reading
def face_summary(conn, root_id=None, detect_video_frames: int = 0) -> dict:
    from ..faces import backend as fb

    rc, rp = _root_clause(root_id)
    # Face detection only runs on canonical (non-duplicate) media, so every
    # count here is over _NOT_HIDDEN, the "total" is unique media, matching
    # the people grid and what actually gets scanned. Videos only count
    # once the detect stage actually processes them (detect_video_frames >
    # 0) — otherwise this would claim videos are "unscanned" forever even
    # though the stage deliberately skips them while the feature is off.
    media_types = "('image','video')" if detect_video_frames > 0 else "('image')"
    total_images = conn.execute(
        f"SELECT COUNT(*) FROM files f WHERE {_NOT_HIDDEN} AND f.media_type IN {media_types}{rc}",
        rp,
    ).fetchone()[0]
    scanned = conn.execute(
        f"""SELECT COUNT(*) FROM files f JOIN face_scan s ON s.file_id=f.id
            WHERE {_NOT_HIDDEN} AND f.media_type IN {media_types}{rc}""",
        rp,
    ).fetchone()[0]
    faces = conn.execute(
        f"""SELECT COUNT(*) FROM faces fa JOIN files f ON f.id=fa.file_id
            WHERE {_NOT_HIDDEN} AND fa.not_person=0 AND {_QUALITY_OK}{rc}""",
        rp,
    ).fetchone()[0]
    people = conn.execute(
        f"""SELECT COUNT(DISTINCT fa.person_id) FROM faces fa
            JOIN files f ON f.id=fa.file_id
            WHERE {_NOT_HIDDEN} AND fa.person_id IS NOT NULL AND {_QUALITY_OK}{rc}""",
        rp,
    ).fetchone()[0]
    photos_with_faces = conn.execute(
        f"""SELECT COUNT(DISTINCT fa.file_id) FROM faces fa
            JOIN files f ON f.id=fa.file_id
            WHERE {_NOT_HIDDEN} AND fa.not_person=0 AND {_QUALITY_OK}{rc}""",
        rp,
    ).fetchone()[0]
    return {
        "total_images": total_images,
        "scanned": scanned,
        "unscanned": max(0, total_images - scanned),
        "faces": faces,
        "people": people,
        "photos_with_faces": photos_with_faces,
        "backend_available": fb.available(),
    }


def _preview_faces(conn, pids, k=4) -> dict:
    """Up to k sharpest (highest det_score), non-hidden face ids per person, for
    the 4-up collage on each person card. One window-function query for the page."""
    pids = [p for p in pids if p is not None]
    if not pids:
        return {}
    marks = ",".join("?" * len(pids))
    rows = conn.execute(
        f"""SELECT person_id, id FROM (
                SELECT fa.id, fa.person_id,
                       ROW_NUMBER() OVER (PARTITION BY fa.person_id
                                          ORDER BY fa.det_score DESC, fa.id) rn
                FROM faces fa JOIN files f ON f.id=fa.file_id
                WHERE fa.person_id IN ({marks}) AND f.hidden=0 AND {_QUALITY_OK}
            ) WHERE rn <= ?""",
        (*pids, k),
    ).fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r["person_id"], []).append(r["id"])
    return out


@reading
def face_persons(conn, root_id=None, limit=120, offset=0) -> dict:
    """People (clusters) in this archive, named people first and then most faces.
    Each carries up to 4 preview faces for a collage card + photo/face counts."""
    rc, rp = _root_clause(root_id)
    rows = conn.execute(
        f"""SELECT fa.person_id pid, p.name, p.cover_face_id,
                   COUNT(DISTINCT fa.file_id) photos, COUNT(*) faces
            FROM faces fa JOIN files f ON f.id=fa.file_id
            JOIN persons p ON p.id=fa.person_id
            WHERE {_NOT_HIDDEN} AND fa.person_id IS NOT NULL AND {_QUALITY_OK}{rc}
            GROUP BY fa.person_id
            ORDER BY CASE WHEN NULLIF(TRIM(p.name), '') IS NULL THEN 1 ELSE 0 END,
                     faces DESC, pid
            LIMIT ? OFFSET ?""",
        (*rp, limit, offset),
    ).fetchall()
    prev = _preview_faces(conn, [r["pid"] for r in rows])
    # Manual-only tags (person_files) aren't reachable from the faces JOIN
    # above, so a person's card would undercount photos for anyone also
    # tagged by hand on a file with no detected face of them. Rather than
    # reshape the main query (see the long comment in media() about why a
    # correlated EXISTS over this archive is a trap), do a second small
    # query scoped to just this page's person ids, counting only files
    # NOT already reachable through faces for that same person so a file
    # tagged both ways still counts once.
    pids = [r["pid"] for r in rows]
    manual_counts: dict[int, int] = {}
    if pids:
        marks = ",".join("?" for _ in pids)
        rc2 = rc.replace("f.root_id", "f2.root_id") if rc else rc
        manual_rows = conn.execute(
            f"""SELECT pf.person_id pid, COUNT(*) c
                FROM person_files pf JOIN files f2 ON f2.id=pf.file_id
                WHERE pf.person_id IN ({marks})
                  AND f2.present=1 AND f2.hidden=0{rc2}
                  AND pf.file_id NOT IN (
                      SELECT fa2.file_id FROM faces fa2
                      WHERE fa2.person_id=pf.person_id)
                GROUP BY pf.person_id""",
            (*pids, *rp),
        ).fetchall()
        manual_counts = {r["pid"]: r["c"] for r in manual_rows}
    people = [
        {
            "id": r["pid"],
            "name": r["name"],
            "cover_face_id": r["cover_face_id"],
            "faces_preview": prev.get(r["pid"], []),
            "photos": r["photos"] + manual_counts.get(r["pid"], 0),
            "faces": r["faces"],
        }
        for r in rows
    ]
    return {"people": people, "offset": offset, "count": len(people)}


@reading
def face_person(conn, person_id: int, root_id=None, limit=120, offset=0) -> dict | None:
    """Files this person appears in: files with a detected face of them,
    UNION ALL files manually tagged with them (person_files) that don't
    already have such a face -- so a file tagged both ways appears once, and
    manual-only items carry face_id=None."""
    p = conn.execute("SELECT id, name FROM persons WHERE id=?", (person_id,)).fetchone()
    if not p:
        return None
    rc, rp = _root_clause(root_id)
    rows = conn.execute(
        f"""SELECT id, media_type, rel_path, dt, dsrc, has_gps, face_id FROM (
                SELECT f.id AS id, f.media_type, f.rel_path,
                       d.best_datetime AS dt, d.date_source AS dsrc,
                       EXISTS(SELECT 1 FROM geo g WHERE g.file_id=f.id) AS has_gps,
                       (SELECT fa2.id FROM faces fa2
                        WHERE fa2.file_id=f.id AND fa2.person_id=?
                          AND {_quality_ok("fa2")}
                        ORDER BY fa2.det_score DESC LIMIT 1) AS face_id
                FROM faces fa JOIN files f ON f.id=fa.file_id
                LEFT JOIN dates d ON d.file_id=f.id
                WHERE fa.person_id=? AND {_NOT_HIDDEN} AND {_QUALITY_OK}{rc}
                GROUP BY f.id
                UNION ALL
                SELECT f.id AS id, f.media_type, f.rel_path,
                       d.best_datetime AS dt, d.date_source AS dsrc,
                       EXISTS(SELECT 1 FROM geo g WHERE g.file_id=f.id) AS has_gps,
                       NULL AS face_id
                FROM person_files pf JOIN files f ON f.id=pf.file_id
                LEFT JOIN dates d ON d.file_id=f.id
                WHERE pf.person_id=? AND {_NOT_HIDDEN}{rc}
                  AND pf.file_id NOT IN (
                      SELECT fa2.file_id FROM faces fa2 WHERE fa2.person_id=?)
            )
            ORDER BY (dt IS NULL), dt DESC, id
            LIMIT ? OFFSET ?""",
        (person_id, person_id, *rp, person_id, *rp, person_id, limit, offset),
    ).fetchall()
    total = conn.execute(
        f"""SELECT
                (SELECT COUNT(DISTINCT fa.file_id) FROM faces fa
                 JOIN files f ON f.id=fa.file_id
                 WHERE fa.person_id=? AND {_NOT_HIDDEN} AND {_QUALITY_OK}{rc})
              + (SELECT COUNT(*) FROM person_files pf
                 JOIN files f ON f.id=pf.file_id
                 WHERE pf.person_id=? AND {_NOT_HIDDEN}{rc}
                   AND pf.file_id NOT IN (
                       SELECT fa2.file_id FROM faces fa2 WHERE fa2.person_id=?))""",
        (person_id, *rp, person_id, *rp, person_id),
    ).fetchone()[0]
    items = [
        {
            "id": r["id"],
            "type": r["media_type"],
            "name": os.path.basename(r["rel_path"]),
            "date": r["dt"],
            "date_source": r["dsrc"],
            "has_gps": bool(r["has_gps"]),
            "face_id": r["face_id"],
        }
        for r in rows
    ]
    return {
        "id": person_id,
        "name": p["name"],
        "photos": total,
        "items": items,
        "offset": offset,
        "count": len(items),
        "merges": _person_merges_for(conn, person_id, p["name"]),
    }


def _person_merges_for(conn, person_id, name) -> list[dict]:
    """Merges this person can undo. Looked up by survivor_id OR by
    survivor_name (when non-empty) rather than survivor_id alone: cluster_faces
    DELETEs and rebuilds every `persons` row, so a merge's survivor_id can
    rot after a recluster -- the name is the durable anchor, same reasoning
    as person_files (see repair_manual_person_files)."""
    name = name or ""
    rows = conn.execute(
        """SELECT id, dropped_name, face_ids, created_at FROM person_merges
           WHERE survivor_id=? OR (? != '' AND survivor_name=?)
           ORDER BY created_at DESC""",
        (person_id, name, name),
    ).fetchall()
    out = []
    for row in rows:
        fids = json.loads(row["face_ids"])
        photos = 0
        if fids:
            marks = ",".join("?" for _ in fids)
            photos = conn.execute(
                f"SELECT COUNT(DISTINCT file_id) FROM faces WHERE id IN ({marks})", fids
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
def rename_person(conn, person_id, name: str) -> dict:
    if not person_id:
        return {"error": "missing person_id"}
    old = conn.execute("SELECT name FROM persons WHERE id=?", (person_id,)).fetchone()
    if not old:
        return {"error": "not found"}
    conn.execute("UPDATE persons SET name=? WHERE id=?", (name or None, person_id))
    # Keep manual face-pins (stored by NAME) tracking the rename so re-clustering
    # still lands pinned faces on this person.
    if old["name"] and name and old["name"] != name:
        conn.execute("UPDATE faces SET manual_person=? WHERE manual_person=?", (name, old["name"]))
    conn.commit()
    return {"ok": True, "name": name or None}


# -- in-panel edits (face / person) ------------------------------------------


def _sync_person_stats(conn, pid):
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


@writing
def reassign_face(conn, face_id, person_id) -> dict:
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
def detach_file_from_person(conn, person_id, file_id) -> dict:
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
def add_person_to_file(conn, person_id, file_id) -> dict:
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
    conn.commit()
    return {"ok": True, "person": {"id": p["id"], "name": p["name"]}}


@writing
def remove_person_from_file(conn, person_id, file_id) -> dict:
    if not person_id or not file_id:
        return {"error": "missing person_id or file_id"}
    conn.execute("DELETE FROM person_files WHERE person_id=? AND file_id=?", (person_id, file_id))
    conn.commit()
    return {"ok": True}


# -- "same person?" review: merges + constraints ---------------------------

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


def _rep_face(conn, pid, cover) -> int | None:
    """A stable representative face id for a person (its cover, or its sharpest
    face). Used to anchor a durable face_links constraint."""
    if cover:
        return cover
    r = conn.execute(
        f"SELECT id FROM faces WHERE person_id=? AND {_quality_ok('faces')} "
        f"ORDER BY det_score DESC LIMIT 1",
        (pid,),
    ).fetchone()
    return r["id"] if r else None


def _update_person_centroid(conn, pid) -> None:
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


def _finish_person_merge(conn, keep, survivor_name) -> None:
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
def merge_persons(conn, id_a, id_b, name=None) -> dict:
    """User confirmed two clusters are the same person. Merge immediately (move
    faces, keep the named/larger one) AND store a durable 'same' constraint so
    the merge survives future re-clusters.

    An explicit `name` picks the surviving NAME outright (it doesn't change
    which person id survives -- that's still named/larger-cluster/id as
    below). This is what lets two already-but-differently-named clusters
    merge at all: normally that's refused because there's no automatic way to
    choose between them."""
    pa, pb, err = merging.load_sides(conn, _PERSON.entity, id_a, id_b)
    if err:
        return err
    name, err = merging.resolve_name(pa, pb, name)
    if err:
        return err
    # keep the named one, else the larger cluster
    if pa["name"] and not pb["name"]:
        keep, drop = pa, pb
    elif pb["name"] and not pa["name"]:
        keep, drop = pb, pa
    elif (pa["face_count"] or 0) >= (pb["face_count"] or 0):
        keep, drop = pa, pb
    else:
        keep, drop = pb, pa
    survivor_name = name or keep["name"]
    merging.merge_linked(
        conn,
        _PERSON,
        keep,
        drop,
        survivor_name=survivor_name,
        rep=lambda c, side: _rep_face(c, side["id"], side["cover_face_id"]),
        finish=_finish_person_merge,
        now=db.now_iso(),
    )
    conn.commit()
    r = conn.execute("SELECT id,name,face_count FROM persons WHERE id=?", (keep["id"],)).fetchone()
    return {
        "ok": True,
        "person": {"id": r["id"], "name": r["name"], "face_count": r["face_count"]},
    }


def _restore_person_pins(conn, m) -> None:
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
def unmerge_persons(conn, merge_id) -> dict:
    """Undo a drag-merge recorded by merge_persons. See merging.unmerge_linked
    for the cannot-link mechanism this writes, its known limitation, and the
    "safe to call twice" / recluster-return contract.

    People-specific: this also restores names. Any recorded face whose
    manual_person currently equals the survivor's name is pinned back to the
    dropped name (or NULL if the dropped side was unnamed), or
    faces/cluster.py's _apply_manual_pins would keep re-pinning it onto the
    survivor forever. See _finish_person_merge's "Load-bearing, not cosmetic"
    comment -- this undoes exactly that."""
    return merging.unmerge_linked(conn, _PERSON, merge_id, restore=_restore_person_pins)


@writing
def _persons_link(conn, id_a, id_b, kind: str) -> dict:
    """Record a durable pairwise constraint between two clusters (by their
    representative faces). 'different' = cannot-link (blocks future auto-merge);
    'skip' = "reviewed, undecided" (just drops the pair from the queue so it stops
    coming back). Neither changes the current clustering."""
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


def set_persons_different(db_path: str, id_a, id_b) -> dict:
    return _persons_link(db_path, id_a, id_b, "different")


def set_persons_skip(db_path: str, id_a, id_b) -> dict:
    return _persons_link(db_path, id_a, id_b, "skip")


@writing
def hide_person(conn, person_id, kind="false_detection") -> dict:
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


@reading
def person_suggestions(conn, root_id=None, limit=40, min_sim=0.45) -> dict:
    """Top candidate "same person?" pairs: distinct clusters whose centroids are
    >= min_sim cosine, highest first, excluding pairs the user already answered
    'different'. This is the review queue, the pairs the automatic pass left
    apart but that look like they could be one person."""
    import numpy as np

    rows = conn.execute(
        "SELECT id,name,cover_face_id,face_count,centroid FROM persons "
        "WHERE centroid IS NOT NULL AND face_count > 0"
    ).fetchall()
    if len(rows) < 2:
        return {"suggestions": []}
    ids = [r["id"] for r in rows]
    C = np.array([np.frombuffer(r["centroid"], "float32") for r in rows], dtype="float32")
    C /= np.linalg.norm(C, axis=1, keepdims=True) + 1e-9
    S = C @ C.T
    iu = np.triu_indices(len(ids), 1)
    sims = S[iu]
    keep = sims >= min_sim
    ii, jj, ss = iu[0][keep], iu[1][keep], sims[keep]
    order = np.argsort(-ss)
    # person-pairs the user already answered 'different' (cannot-link) or
    # 'skip' (reviewed, undecided), both are removed from the queue so it
    # stops resurfacing the same pairs.
    excl = set()
    for lk in conn.execute(
        "SELECT face_a,face_b FROM face_links WHERE kind IN ('different','skip')"
    ):
        fa = conn.execute("SELECT person_id FROM faces WHERE id=?", (lk["face_a"],)).fetchone()
        fb = conn.execute("SELECT person_id FROM faces WHERE id=?", (lk["face_b"],)).fetchone()
        if fa and fb and fa[0] and fb[0]:
            excl.add(frozenset((fa[0], fb[0])))
    info = {r["id"]: r for r in rows}
    out, total = [], 0
    for o in order:
        ia, ib = ids[int(ii[o])], ids[int(jj[o])]
        if frozenset((ia, ib)) in excl:
            continue
        total += 1  # count every un-answered candidate
        if len(out) >= limit:
            continue
        ra, rb = info[ia], info[ib]
        out.append(
            {
                "sim": round(float(ss[o]), 3),
                "a": {
                    "id": ia,
                    "name": ra["name"],
                    "cover_face_id": ra["cover_face_id"],
                    "faces": ra["face_count"],
                },
                "b": {
                    "id": ib,
                    "name": rb["name"],
                    "cover_face_id": rb["cover_face_id"],
                    "faces": rb["face_count"],
                },
            }
        )
    prev = _preview_faces(conn, [x["a"]["id"] for x in out] + [x["b"]["id"] for x in out])
    for x in out:
        x["a"]["faces_preview"] = prev.get(x["a"]["id"], [])
        x["b"]["faces_preview"] = prev.get(x["b"]["id"], [])
    return {"suggestions": out, "total": total}


@reading
def face_crop_source(conn, face_id: int):
    """(abs path, sha256, box, rotate_deg, frame_offset, media_type, file_id)
    for a face id, or None. ``file_id`` is the underlying file's id, distinct
    from ``face_id``, for keying a re-derived video frame the same way
    regardless of which face on that frame asked for it.

    Path is DB-derived. ``rotate_deg`` is the turn the photo needs on top of its
    EXIF orientation — the box was detected in that rotated frame, so a crop has
    to rotate before it cuts. ``frame_offset`` is the ffmpeg ``-ss`` offset of
    the sampled video keyframe this face was detected in, or None for a photo
    (in which case ``rotate_deg`` still applies and ``media_type`` is
    'image'); a video detection's box is instead in that *extracted frame's*
    own pixel coordinates and is already upright (ffmpeg applies container
    rotation on extraction), so callers must not also rotate it."""
    r = conn.execute(
        """SELECT r.path AS root, f.rel_path, f.sha256, f.media_type, f.id AS file_id,
                  fa.box_x, fa.box_y, fa.box_w, fa.box_h, fa.frame_offset,
                  COALESCE(o.rotate_deg, 0) AS rotate_deg
           FROM faces fa JOIN files f ON f.id=fa.file_id
           JOIN roots r ON r.id=f.root_id
           LEFT JOIN orientation o ON o.file_id=f.id
           WHERE fa.id=?""",
        (face_id,),
    ).fetchone()
    if not r:
        return None
    p = Path(r["root"]) / r["rel_path"]
    if not p.is_file():
        return None
    return (
        p,
        r["sha256"],
        (r["box_x"], r["box_y"], r["box_w"], r["box_h"]),
        r["rotate_deg"],
        r["frame_offset"],
        r["media_type"],
        r["file_id"],
    )
