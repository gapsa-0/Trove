"""Pets, read side: what the Pets panel, a pet's page and the review lists show.

Everything a user can *change* lives in ``pets_edit.py``; see
``services/people.py`` for why the two are split, since this mirrors it.

Mirrors faces/people end to end -- `pets/cluster.py:cluster_pets` plays the
same role as `faces/cluster.py:cluster_faces`, `pet_links` the same role as
`face_links`, and `pet_merges` the same role as `person_merges`. Where a
function has a person-side counterpart that matters, its docstring names it.

Same two-sources rule as People: a pet's photos come from its detections
(`animal_detections.pet_id`) and its manual tags (`pet_files`), unioned so a
file tagged both ways still counts once.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from ._common import _NOT_HIDDEN, _root_clause, reading
from .types import MediaItem

# -- pets / non-human detections -------------------------------------------


@reading
def pet_summary(
    conn: sqlite3.Connection,
    root_id: int | None = None,
    model_source: str | None = None,
    detect_video_frames: int = 0,
) -> dict[str, Any]:
    """The Pets panel's summary tile: scanned/unscanned image counts, animal
    detections, named pet groups, non-human faces, and whether the pet
    backend is available."""
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
    scan_params: list[Any] = list(rp)
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


def _preview_detections(
    conn: sqlite3.Connection, pids: list[int | None], k: int = 4
) -> dict[int, list[int]]:
    """Up to k detection ids per pet for the collage on its card: the one they
    chose as the cover, then the best-scoring of the rest.

    The people-side twin is ``people._preview_faces``; a pet's card used to show
    a single thumbnail, which said less about a group of twenty photos than four
    of them do."""
    pids = [p for p in pids if p is not None]
    if not pids:
        return {}
    marks = ",".join("?" * len(pids))
    rows = conn.execute(
        f"""SELECT pet_id, id FROM (
                SELECT a.id, a.pet_id,
                       ROW_NUMBER() OVER (PARTITION BY a.pet_id
                                          ORDER BY a.manual_cover DESC,
                                                   a.det_score DESC, a.id) rn
                FROM animal_detections a JOIN files f ON f.id=a.file_id
                WHERE a.pet_id IN ({marks}) AND f.hidden=0
            ) WHERE rn <= ?""",
        (*pids, k),
    ).fetchall()
    out: dict[int, list[int]] = {}
    for r in rows:
        out.setdefault(r["pet_id"], []).append(r["id"])
    return out


@reading
def pet_groups(
    conn: sqlite3.Connection, root_id: int | None = None, limit: int = 120, offset: int = 0
) -> dict[str, Any]:
    """Pet clusters (identities) in this archive, named ones first and then
    most detections, each with its detection/photo counts."""
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
    prev = _preview_detections(conn, pids)
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
                "detections_preview": prev.get(row["pet_id"], []),
                "detections": row["detections"],
                "photos": row["photos"] + manual_counts.get(row["pet_id"], 0),
            }
            for row in rows
        ],
        "offset": offset,
        "count": len(rows),
    }


@reading
def animal_gallery(
    conn: sqlite3.Connection,
    root_id: int | None = None,
    limit: int = 120,
    offset: int = 0,
    unassigned: bool = False,
) -> dict[str, Any]:
    """A page of raw animal detections (teddy bears excluded), optionally
    filtered to ones not yet assigned to a pet cluster."""
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
def pet_group(
    conn: sqlite3.Connection,
    pet_id: int,
    root_id: int | None = None,
    limit: int = 120,
    offset: int = 0,
) -> dict[str, Any] | None:
    """Files this pet appears in: files with a detection of them, UNION ALL
    files manually tagged with them (pet_files) that don't already have such
    a detection -- mirrors face_person. Manual-only items carry
    detection_id=None."""
    pet = conn.execute(
        "SELECT id,name,species,cover_detection_id FROM pets WHERE id=?", (pet_id,)
    ).fetchone()
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
    items: list[MediaItem] = [
        {
            "id": row["id"],
            "name": os.path.basename(row["rel_path"]),
            "date": row["dt"],
            "detection_id": row["detection_id"],
            "type": "image",
            "has_gps": False,
        }
        for row in rows
    ]
    return {
        "id": pet["id"],
        "name": pet["name"],
        "species": pet["species"],
        # What the page's portrait draws, so choosing a cover shows somewhere.
        "cover_detection_id": pet["cover_detection_id"],
        "photos": total,
        "items": items,
        "offset": offset,
        "count": len(rows),
        "merges": _pet_merges_for(conn, pet_id, pet["name"]),
    }


def _pet_merges_for(
    conn: sqlite3.Connection, pet_id: int, name: str | None
) -> list[dict[str, Any]]:
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


@reading
def nonhuman_review(
    conn: sqlite3.Connection, root_id: int | None = None, limit: int = 120, offset: int = 0
) -> dict[str, Any]:
    """A page of non-human face detections (dolls, animals, cartoons) awaiting
    or already given a review verdict, highest confidence first."""
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


@reading
def animal_crop_source(
    conn: sqlite3.Connection, detection_id: int
) -> tuple[Path, str, tuple[int, int, int, int], int, str | None, str, int] | None:
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
