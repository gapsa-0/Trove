"""Writing one file's detections, as a unit.

Split out of ``extract.py`` because a detect pass has two separable jobs --
deciding what is in a photo, and recording it -- and only the second one needs
to know the shape of five tables. It also gave the file the room to fix the
bug documented under ``_save_suppressed`` below.

**A rewrite is wholesale.** Re-detecting a file deletes its faces, animals,
suppressed candidates and orientation, then writes what this pass found. That
is what makes a detector or config change take full effect rather than
layering new rows on stale ones. The one thing that must survive it is a
*human's* decision -- see ``_carry_reviews``.
"""

from __future__ import annotations


def rewrite_file_detections(conn, fid, now, result, pet_src, fiqa_model, sha256):
    """Replace every detection row for one file with this pass's findings."""
    carried = _carry_reviews(conn, fid)
    conn.execute("DELETE FROM faces WHERE file_id=?", (fid,))
    conn.execute("DELETE FROM animal_detections WHERE file_id=?", (fid,))
    conn.execute("DELETE FROM nonhuman_detections WHERE file_id=?", (fid,))
    conn.execute("DELETE FROM orientation WHERE file_id=?", (fid,))
    if result.rotate:
        conn.execute(
            """INSERT INTO orientation
               (file_id, rotate_deg, source, confidence, created_at)
               VALUES (?,?,?,?,?)""",
            (fid, result.rotate, result.orient_source, result.confidence, now),
        )
    animal_ids = _save_animals(conn, fid, now, result, pet_src)
    _save_faces(conn, fid, now, result, fiqa_model)
    _save_suppressed(conn, fid, now, result, animal_ids, carried, sha256)


def _carry_reviews(conn, fid) -> dict[tuple[int, int, int, int], str]:
    """This file's already-reviewed suppressions, keyed by box.

    A rescan re-runs the animal veto and would suppress the same face again --
    including one the user has already told us is a person. Their answer is not
    something the detector may overwrite, so it is carried across the rewrite
    and re-applied to the box it was given for. Keyed on the box rather than the
    row id because the row is about to be deleted and re-inserted; a detector
    that moves the box a few pixels loses the carry, which is the safe way to be
    wrong (the candidate simply returns to the review queue).
    """
    rows = conn.execute(
        """SELECT box_x, box_y, box_w, box_h, review_status
           FROM nonhuman_detections WHERE file_id=? AND review_status != 'pending'""",
        (fid,),
    ).fetchall()
    return {(r["box_x"], r["box_y"], r["box_w"], r["box_h"]): r["review_status"] for r in rows}


def _save_animals(conn, fid, now, result, pet_src) -> list[int]:
    """Insert this file's animals, returning their new row ids in order."""
    ids = []
    for a, offset in result.animal_hits:
        cur = conn.execute(
            """INSERT INTO animal_detections
               (file_id,species,box_x,box_y,box_w,box_h,det_score,
                embedding,model_source,frame_offset,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                fid,
                a.species,
                a.x,
                a.y,
                a.w,
                a.h,
                a.score,
                a.embedding.tobytes(),
                pet_src,
                offset,
                now,
            ),
        )
        ids.append(cur.lastrowid)
    return ids


def _save_faces(conn, fid, now, result, fiqa_model) -> None:
    for fc, offset in result.face_hits:
        conn.execute(
            """INSERT INTO faces
               (file_id, box_x, box_y, box_w, box_h, det_score,
                focus_score, brightness, extreme_fraction,
                clipped_fraction, quality_score, quality_source,
                fiqa_norm, fiqa_score, fiqa_source, quality_tier,
                embedding, frame_offset, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                fid,
                fc.x,
                fc.y,
                fc.w,
                fc.h,
                fc.score,
                fc.focus_score,
                fc.brightness,
                fc.extreme_fraction,
                fc.clipped_fraction,
                fc.quality_score,
                fc.quality_source,
                # getattr, like _best_face_quality: lightweight stand-in
                # backends (tests, third-party) yield objects without the FIQA
                # fields, and an un-tiered face is a supported state (NULL
                # reads as BORDERLINE downstream).
                getattr(fc, "fiqa_norm", None),
                getattr(fc, "fiqa_score", None),
                fiqa_model,
                getattr(fc, "quality_tier", None),
                fc.embedding.tobytes(),
                offset,
                now,
            ),
        )


def _save_suppressed(conn, fid, now, result, animal_ids, carried, sha256) -> None:
    """Record the faces the animal veto dropped, so they stay reviewable.

    Without this the veto is unappealable. A face inside an animal box is
    discarded on the detector's word alone, and a user who can see it is their
    child in front of the dog has nothing to click: `nonhuman_detections` is
    what the Pets screen's review queue reads, what `services/pets.py`'s
    `review_nonhuman` marks, and what it rebuilds a `faces` row from when the
    answer is "human" -- which is why the box, scores and embedding are stored
    here rather than re-derived later.

    This was lost when people and pets were fused into one pass (67a2c5c): the
    fused detector counted the suppression and deleted the table's rows per
    file, but nothing ever inserted into it again, so the queue was
    permanently empty and every vetoed face was silently unrecoverable.
    """
    for fc, animal_index in result.suppressed_hits:
        box = (fc.x, fc.y, fc.w, fc.h)
        status = carried.get(box, "pending")
        cur = conn.execute(
            """INSERT INTO nonhuman_detections
               (file_id,animal_detection_id,box_x,box_y,box_w,box_h,kind,
                confidence,source,source_sha256,embedding,det_score,focus_score,
                brightness,extreme_fraction,clipped_fraction,quality_score,
                quality_source,review_status,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                fid,
                animal_ids[animal_index] if animal_index < len(animal_ids) else None,
                fc.x,
                fc.y,
                fc.w,
                fc.h,
                "animal",
                fc.score,
                "animal-overlap",
                sha256,
                fc.embedding.tobytes(),
                fc.score,
                fc.focus_score,
                fc.brightness,
                fc.extreme_fraction,
                fc.clipped_fraction,
                fc.quality_score,
                fc.quality_source,
                status,
                now,
            ),
        )
        if status == "human":
            _restore_reviewed_face(conn, fid, fc, cur.lastrowid, now)


def _restore_reviewed_face(conn, fid, fc, detection_id, now) -> None:
    """Put back a face the user has already ruled human.

    The rewrite deleted this file's faces, including one an earlier review had
    restored, and the veto has just suppressed the same box again. Carrying
    only the review *status* would leave the person missing from People until
    someone reviewed them a second time -- and the old ``restored_face_id``
    cannot simply be carried either: it points at a row this rewrite deleted,
    which is a foreign-key violation, and the failure surfaces as "detection
    failed" on a file that detected perfectly well.

    So the face is re-created from the detection's own columns (the same
    columns ``services/pets.py`` rebuilds one from) and the candidate is
    pointed at the new row.
    """
    face = conn.execute(
        """INSERT INTO faces
           (file_id,box_x,box_y,box_w,box_h,det_score,focus_score,brightness,
            extreme_fraction,clipped_fraction,quality_score,quality_source,
            embedding,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            fid,
            fc.x,
            fc.y,
            fc.w,
            fc.h,
            fc.score,
            fc.focus_score,
            fc.brightness,
            fc.extreme_fraction,
            fc.clipped_fraction,
            fc.quality_score,
            fc.quality_source,
            fc.embedding.tobytes(),
            now,
        ),
    )
    conn.execute(
        "UPDATE nonhuman_detections SET restored_face_id=? WHERE id=?",
        (face.lastrowid, detection_id),
    )
