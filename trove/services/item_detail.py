"""One file's detail panel: everything the viewer says about a single item.

Split out of ``browse.py`` because it answers a different question. That module
is about *finding* files -- the grid, the filters, the counts -- and works in
whole result sets; this one is about one file, and grew the moment the panel
started reporting provenance, coverage and what was read out of a file rather
than just its size and dimensions.

Two rules shape what is returned, and both exist because the panel's wording
depends on them:

* **Coverage is reported separately from findings.** ``read`` says which stages
  have looked at this file at all. "Found nothing" and "not looked at yet" are
  different facts, and an archive mid-pipeline is mostly the second -- so the
  panel is given what it needs to tell them apart instead of having to guess
  from an empty list.
* **A document's text is never sent, a picture's always is.** The transcript of
  a scan is the point of reading it; the text of a forty-page contract is
  thousands of words nobody wants in a side panel, and the document itself is
  what the viewer puts on screen. See ``_item_text``.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any, cast

from ..db import database as db
from . import text_search
from ._common import _NOT_HIDDEN, _QUALITY_OK, reading
from .places import _PLACE_EXEMPT


def _item_detections(
    conn: sqlite3.Connection, fid: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Detected people and animals for one file, as (people, animals)."""
    people = [
        {
            "person_id": r["person_id"],
            "name": r["name"],
            "face_id": r["face_id"],
            # Where the face is, so the viewer can draw it on the photo. Stored
            # in ORIGINAL-image pixels with the detected rotation already
            # applied -- the same frame `/file/` serves the photo in -- so the
            # box needs no correction on the way to the screen. A video
            # detection's box was measured in an extracted keyframe, not in the
            # file, so it would not line up with anything and is left off.
            "box": (
                None
                if r["frame_offset"] is not None
                else {"x": r["box_x"], "y": r["box_y"], "w": r["box_w"], "h": r["box_h"]}
            ),
        }
        for r in conn.execute(
            f"""SELECT fa.id AS face_id, fa.person_id, p.name, fa.frame_offset,
                       fa.box_x, fa.box_y, fa.box_w, fa.box_h
           FROM faces fa LEFT JOIN persons p ON p.id=fa.person_id
           WHERE fa.file_id=? AND fa.not_person=0 AND {_QUALITY_OK}
           ORDER BY fa.det_score DESC""",
            (fid,),
        )
    ]
    animals = [
        {
            "detection_id": row["detection_id"],
            "species": row["species"],
            "pet_id": row["pet_id"],
            "name": row["name"],
            "score": row["det_score"],
        }
        for row in conn.execute(
            """SELECT a.id detection_id,a.species,a.pet_id,p.name,a.det_score
           FROM animal_detections a LEFT JOIN pets p ON p.id=a.pet_id
           WHERE a.file_id=? AND a.species!='teddy bear'
           ORDER BY a.det_score DESC""",
            (fid,),
        )
    ]
    return people, animals


def _item_place(conn: sqlite3.Connection, fid: int, min_media: int) -> sqlite3.Row | None:
    # Current place membership (a file belongs to at most one place).
    # A below-threshold, unnamed/unpinned cluster is not reported as a
    # "place" here either (see place_min_media) — this file just has no
    # location, matching what place_clusters() shows on the map.
    row = conn.execute(
        f"""SELECT pc.id, pc.name FROM place_cluster_members pcm
           JOIN place_clusters pc ON pc.id=pcm.cluster_id
           WHERE pcm.file_id=? AND (pc.member_count >= ? OR {_PLACE_EXEMPT})
           LIMIT 1""",
        (fid, min_media),
    ).fetchone()
    # sqlite3.Cursor.fetchone() is typed Any, so say what this one returns.
    return cast("sqlite3.Row | None", row)


def _item_pick_lists(
    conn: sqlite3.Connection, root_id: int | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    # Pick-lists for in-panel editing: only *named* places (in this file's root)
    # and *named* persons are offered as targets.
    place_options = [
        {"id": r["id"], "name": r["name"]}
        for r in conn.execute(
            """SELECT id, name FROM place_clusters
           WHERE root_id=? AND name IS NOT NULL
           ORDER BY name COLLATE NOCASE""",
            (root_id,),
        )
    ]
    person_options = [
        {"id": r["id"], "name": r["name"]}
        for r in conn.execute(
            """SELECT id, name FROM persons WHERE name IS NOT NULL AND name != ''
           ORDER BY name COLLATE NOCASE"""
        )
    ]
    pet_options = [
        {"id": r["id"], "name": r["name"]}
        for r in conn.execute(
            """SELECT id, name FROM pets WHERE name IS NOT NULL AND name != ''
           ORDER BY name COLLATE NOCASE"""
        )
    ]
    return place_options, person_options, pet_options


def _item_manual_tags(
    conn: sqlite3.Connection, fid: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    # Manually tagged people/pets on this file (person_files/pet_files) --
    # no face/detection exists for these, so there's no face_id/detection_id.
    # Name resolves from the live persons/pets table; if the id has rotted
    # (a re-cluster ran since and repair hasn't caught up yet) fall back to
    # the name stored on the row itself.
    manual_people = [
        {
            "person_id": r["person_id"],
            "name": r["name"] or r["person_name"],
        }
        for r in conn.execute(
            """SELECT pf.person_id, pf.person_name, p.name
           FROM person_files pf LEFT JOIN persons p ON p.id=pf.person_id
           WHERE pf.file_id=?
           ORDER BY COALESCE(p.name, pf.person_name) COLLATE NOCASE""",
            (fid,),
        )
    ]
    manual_pets = [
        {
            "pet_id": r["pet_id"],
            "name": r["name"] or r["pet_name"],
        }
        for r in conn.execute(
            """SELECT pf.pet_id, pf.pet_name, p.name
           FROM pet_files pf LEFT JOIN pets p ON p.id=pf.pet_id
           WHERE pf.file_id=?
           ORDER BY COALESCE(p.name, pf.pet_name) COLLATE NOCASE""",
            (fid,),
        )
    ]
    return manual_people, manual_pets


def _item_coverage(conn: sqlite3.Connection, fid: int) -> dict[str, bool]:
    """Which stages have actually looked at this file yet.

    "Nothing found" and "not looked at yet" are different facts, and the panel
    has to be able to tell them apart -- an archive mid-pipeline is mostly the
    second, and reporting it as the first is a lie the user cannot check.

    Every stage already writes one row per file the moment it looks, including
    for the files it skipped or failed on (that is what makes the passes
    resumable), so a missing row already *means* "not yet" and nothing new has
    to be recorded to answer this. One indexed primary-key probe each.
    """

    def seen(table: str) -> bool:
        return conn.execute(f"SELECT 1 FROM {table} WHERE file_id=?", (fid,)).fetchone() is not None

    return {
        # faces and pets are one fused decode pass but two scan tables, and a
        # file can be covered by one and not the other when a feature is
        # switched on later.
        "people": seen("face_scan"),
        "pets": seen("pet_scan"),
        "text": seen("doc_text"),
        "semantic": seen("semantic_embeddings"),
    }


def _item_text(conn: sqlite3.Connection, fid: int) -> dict[str, Any] | None:
    """What reading this file produced, for the panel's "Detected text" section.

    Two shapes, because two things are wanted. A picture's writing is short and
    the whole point is *what it says*, so the passages are joined back into the
    transcript. A document's text is thousands of words nobody wants in a side
    panel, so it reports only that it was read, by which reader, and how much --
    the document itself is what you read, and the viewer shows it.

    ``reader`` is the feature that produced this text (``ocr`` or
    ``documents``), resolved by the same function the search results use, so a
    row cannot be attributed to one feature here and the other there.
    """
    row = conn.execute("SELECT * FROM doc_text WHERE file_id=?", (fid,)).fetchone()
    if row is None or row["status"] != "extracted":
        return None
    reader = text_search.reader_of(row["extractor"])
    out: dict[str, Any] = {
        "reader": reader,
        "extractor": row["extractor"],
        "chars": row["chars"],
        "pages": row["pages"],
        "confidence": row["confidence"],
    }
    if reader != "ocr":
        return out
    # The text itself lives in the FTS index under the chunk's rowid, so there
    # is one copy of it rather than two (see db/database.py:_migrate_text_index).
    if not db.text_index_present(conn):
        return out
    chunks = conn.execute(
        """SELECT x.text AS body FROM doc_chunks c
             JOIN doc_chunk_fts x ON x.rowid=c.id
            WHERE c.file_id=? ORDER BY c.ordinal""",
        (fid,),
    ).fetchall()
    out["transcript"] = "\n\n".join(r["body"] for r in chunks if r["body"])
    return out


def _item_duplicates(conn: sqlite3.Connection, fid: int) -> dict[str, Any] | None:
    """The duplicate group this file is in, if any."""
    row = conn.execute(
        """SELECT g.id, g.method, g.member_count, m.role
             FROM dup_members m JOIN dup_groups g ON g.id=m.group_id
            WHERE m.file_id=?""",
        (fid,),
    ).fetchone()
    if row is None:
        return None
    return {
        "group_id": row["id"],
        "method": row["method"],
        "count": row["member_count"],
        "canonical": row["role"] == "canonical",
    }


def _item_neighbours(conn: sqlite3.Connection, f: sqlite3.Row) -> dict[str, Any]:
    """How many other present files sit in this file's folder.

    Asked as a RANGE over ``rel_path`` rather than a ``LIKE`` prefix, for two
    reasons that both matter here. ``files`` carries ``UNIQUE (root_id,
    rel_path)``, and a range is the one form that index can serve -- a
    ``LIKE`` carrying an ``ESCAPE`` clause (which it would need, or a folder
    called ``100%`` becomes a pattern) is explicitly excluded from SQLite's
    prefix optimisation and would scan every row of the root instead. This runs
    on every item open, and the viewer is now something you hold an arrow key
    down on, so a full scan per file is the difference between navigation that
    feels instant and navigation that stutters.

    The range alone would also count files in SUBfolders, which are not "in
    this folder"; the ``instr`` term drops them. It runs only on the rows the
    range already narrowed to.
    """
    folder = os.path.dirname(f["rel_path"])
    where = [_NOT_HIDDEN, "f.root_id=?", "f.id<>?"]
    params: list[Any] = [f["root_id"], f["id"]]
    if folder:
        prefix = f"{folder}{os.sep}"
        # The successor of the prefix: same string with its last character
        # bumped by one, which bounds the range without needing an escape.
        where.append("f.rel_path>=? AND f.rel_path<?")
        params += [prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)]
    # No separator left in what follows the prefix == a direct child. For a
    # file at the root the prefix is empty, and this says "has no separator at
    # all", which is the same rule.
    where.append("instr(substr(f.rel_path,?),?)=0")
    params += [len(folder) + 2 if folder else 1, os.sep]
    same_folder = conn.execute(
        f"SELECT COUNT(*) FROM files f WHERE {' AND '.join(where)}", params
    ).fetchone()[0]
    return {"folder": folder, "folder_count": int(same_folder)}


@reading
def item(conn: sqlite3.Connection, fid: int, min_media: int = 10) -> dict[str, Any] | None:
    """The detail-panel payload for one file: its dates, GPS, metadata,
    people/animals, place, and the pick lists to edit them. Returns None if
    ``fid`` doesn't exist."""
    f = conn.execute(
        """SELECT f.*, r.path AS root_path FROM files f
           JOIN roots r ON r.id=f.root_id WHERE f.id=?""",
        (fid,),
    ).fetchone()
    if not f:
        return None
    d = conn.execute("SELECT * FROM dates WHERE file_id=?", (fid,)).fetchone()
    g = conn.execute("SELECT * FROM geo WHERE file_id=?", (fid,)).fetchone()
    m = conn.execute("SELECT * FROM media_meta WHERE file_id=?", (fid,)).fetchone()
    t = conn.execute("SELECT * FROM takeout_sidecar WHERE file_id=?", (fid,)).fetchone()
    people, animals = _item_detections(conn, fid)
    place = _item_place(conn, fid, min_media)
    place_options, person_options, pet_options = _item_pick_lists(conn, f["root_id"])
    manual_people, manual_pets = _item_manual_tags(conn, fid)
    return {
        "id": fid,
        "name": os.path.basename(f["rel_path"]),
        "rel_path": f["rel_path"],
        # The file where it actually is, for "open file location". Sent rather
        # than assembled in the frontend because the root's path is a fact of
        # the archive registry that no screen otherwise carries.
        "abs_path": os.path.join(f["root_path"], f["rel_path"]),
        "type": f["media_type"],
        "size": f["size"],
        "root_id": f["root_id"],
        "date": d["best_datetime"] if d else None,
        "date_source": d["date_source"] if d else None,
        "date_confidence": d["date_confidence"] if d else None,
        "gps": (
            {"lat": g["lat"], "lon": g["lon"], "alt": g["alt"], "source": g["geo_source"]}
            if g
            else None
        ),
        "meta": (dict(m) if m else None),
        "description": (t["description"] if t else None),
        "people": people,
        "animals": animals,
        "place": ({"id": place["id"], "name": place["name"]} if place else None),
        "place_options": place_options,
        "person_options": person_options,
        "pet_options": pet_options,
        "manual_people": manual_people,
        "manual_pets": manual_pets,
        # Which stages have looked at this file, so the panel can say "read,
        # and nothing here" rather than "nothing here" for a file no stage has
        # reached. See _item_coverage.
        "read": _item_coverage(conn, fid),
        "text": _item_text(conn, fid),
        "duplicates": _item_duplicates(conn, fid),
        **_item_neighbours(conn, f),
        # Provenance the panel can put in words. The date's source and the
        # sidecar's match are already resolved facts; they were simply never
        # sent, so the panel printed "mtime" in grey and left the user to know
        # what that meant.
        "takeout": (
            {
                "title": t["title"],
                "match_method": t["match_method"],
                "match_confidence": t["match_confidence"],
            }
            if t
            else None
        ),
        # A file whose sniffed type disagrees with its extension is worth
        # flagging: it is why a "photo" sometimes will not open.
        "type_mismatch": bool(
            m and m["detected_type"] and f["ext"] and m["detected_type"] != f["ext"]
        ),
    }
