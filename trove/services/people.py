"""People, read side: what the People panel, a person's page and the review
queue show.

Everything a user can *change* -- renames, reassignments, detaches, merges and
the durable "same/different" constraints -- lives in ``people_edit.py``. The
split is along that line because the two halves barely overlap: each function
here is one query shaped for one screen, while a mutation there has to keep a
person's faces, stats, centroid and pins consistent as a set.

The one thing to know when reading these queries: a person's photos come from
two places, not one. Detected faces (``faces.person_id``) and manual tags
(``person_files``), unioned so a file tagged both ways still counts once. A
query that forgets the second silently under-counts anyone tagged by hand on a
photo their face was never detected in.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from ._common import _NOT_HIDDEN, _QUALITY_OK, _quality_ok, _root_clause, reading
from .types import MediaItem


@reading
def face_summary(
    conn: sqlite3.Connection, root_id: int | None = None, detect_video_frames: int = 0
) -> dict[str, Any]:
    """The People panel's summary tile: scanned/unscanned image counts, total
    faces and named clusters, and whether the face backend is available."""
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
    # Joined to `persons` only for the hidden flag: this counted straight off
    # `faces` before, which would have kept counting the clusters the grid had
    # stopped showing.
    people = conn.execute(
        f"""SELECT COUNT(DISTINCT fa.person_id) FROM faces fa
            JOIN files f ON f.id=fa.file_id
            JOIN persons p ON p.id=fa.person_id
            WHERE {_NOT_HIDDEN} AND fa.person_id IS NOT NULL AND p.hidden=0
                  AND {_QUALITY_OK}{rc}""",
        rp,
    ).fetchone()[0]
    hidden_people = conn.execute(
        f"""SELECT COUNT(DISTINCT fa.person_id) FROM faces fa
            JOIN files f ON f.id=fa.file_id
            JOIN persons p ON p.id=fa.person_id
            WHERE {_NOT_HIDDEN} AND p.hidden=1 AND {_QUALITY_OK}{rc}""",
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
        "hidden_people": hidden_people,
        "photos_with_faces": photos_with_faces,
        "backend_available": fb.available(),
    }


def _preview_faces(
    conn: sqlite3.Connection, pids: list[int | None], k: int = 4
) -> dict[int, list[int]]:
    """Up to k face ids per person for the 4-up collage on each person card:
    the one they chose as their cover, then the sharpest of the rest. One
    window-function query for the page.

    The chosen cover leads, or picking one would change `persons.cover_face_id`
    and nothing visible: this collage is what a card actually draws, and it
    ranked on det_score alone."""
    pids = [p for p in pids if p is not None]
    if not pids:
        return {}
    marks = ",".join("?" * len(pids))
    rows = conn.execute(
        f"""SELECT person_id, id FROM (
                SELECT fa.id, fa.person_id,
                       ROW_NUMBER() OVER (PARTITION BY fa.person_id
                                          ORDER BY fa.manual_cover DESC,
                                                   fa.det_score DESC, fa.id) rn
                FROM faces fa JOIN files f ON f.id=fa.file_id
                WHERE fa.person_id IN ({marks}) AND f.hidden=0 AND {_QUALITY_OK}
            ) WHERE rn <= ?""",
        (*pids, k),
    ).fetchall()
    out: dict[int, list[int]] = {}
    for r in rows:
        out.setdefault(r["person_id"], []).append(r["id"])
    return out


@reading
def face_persons(
    conn: sqlite3.Connection,
    root_id: int | None = None,
    limit: int = 120,
    offset: int = 0,
    hidden: bool = False,
) -> dict[str, Any]:
    """People (clusters) in this archive, named people first and then most faces.
    Each carries up to 4 preview faces for a collage card + photo/face counts.

    ``hidden`` picks which side of the "hide this person" line to list: the
    grid asks for the visible ones, the Hidden section asks for the rest. One
    query either way, so the two lists cannot drift apart in shape."""
    rc, rp = _root_clause(root_id)
    rows = conn.execute(
        f"""SELECT fa.person_id pid, p.name, p.cover_face_id,
                   COUNT(DISTINCT fa.file_id) photos, COUNT(*) faces
            FROM faces fa JOIN files f ON f.id=fa.file_id
            JOIN persons p ON p.id=fa.person_id
            WHERE {_NOT_HIDDEN} AND fa.person_id IS NOT NULL AND {_QUALITY_OK}
                  AND p.hidden={1 if hidden else 0}{rc}
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


def _person_photo_count(conn: sqlite3.Connection, person_id: int, rc: str, rp: list[int]) -> int:
    """How many files this person appears in, counted the same two ways the
    listing above unions them: files with a detected face of them, plus files
    only tagged with them by hand. Split out of face_person for length; the
    two must stay in step, so a change to one belongs in the other."""
    return int(
        conn.execute(
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
    )


@reading
def face_person(
    conn: sqlite3.Connection,
    person_id: int,
    root_id: int | None = None,
    limit: int = 120,
    offset: int = 0,
) -> dict[str, Any] | None:
    """Files this person appears in: files with a detected face of them,
    UNION ALL files manually tagged with them (person_files) that don't
    already have such a face -- so a file tagged both ways appears once, and
    manual-only items carry face_id=None."""
    # A hidden person's page still opens -- that is where Restore lives, and a
    # link or a history entry can lead here. It reports `hidden` and lets the
    # page say so, rather than 404ing at somebody who exists.
    p = conn.execute(
        "SELECT id, name, hidden, cover_face_id FROM persons WHERE id=?", (person_id,)
    ).fetchone()
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
    total = _person_photo_count(conn, person_id, rc, rp)
    items: list[MediaItem] = [
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
        "hidden": bool(p["hidden"]),
        # What the page's avatar draws. It used to take the first item in the
        # list instead, so choosing a cover changed nothing anyone could see.
        "cover_face_id": p["cover_face_id"],
        "photos": total,
        "items": items,
        "offset": offset,
        "count": len(items),
        "merges": _person_merges_for(conn, person_id, p["name"]),
    }


def _person_merges_for(
    conn: sqlite3.Connection, person_id: int, name: str | None
) -> list[dict[str, Any]]:
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


@reading
def person_suggestions(
    conn: sqlite3.Connection, root_id: int | None = None, limit: int = 40, min_sim: float = 0.45
) -> dict[str, Any]:
    """Top candidate "same person?" pairs: distinct clusters whose centroids are
    >= min_sim cosine, highest first, excluding pairs the user already answered
    'different'. This is the review queue, the pairs the automatic pass left
    apart but that look like they could be one person."""
    import numpy as np

    # Hidden clusters are left out: offering to merge someone you have taken
    # off the screen is asking about a person who is not on it.
    rows = conn.execute(
        "SELECT id,name,cover_face_id,face_count,centroid FROM persons "
        "WHERE centroid IS NOT NULL AND face_count > 0 AND hidden=0"
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
    out: list[dict[str, Any]] = []
    total = 0
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
def face_crop_source(
    conn: sqlite3.Connection, face_id: int
) -> tuple[Path, str, tuple[int, int, int, int], int, str | None, str, int] | None:
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
