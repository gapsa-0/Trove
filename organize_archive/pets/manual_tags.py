"""Manual "this pet is in this photo" tags (``pet_files``).

These tags are anchored by pet NAME rather than id, because clustering
rebuilds `pets` wholesale on every pass -- so they need re-pointing after
each pass.
"""

from __future__ import annotations


def repair_manual_pet_files(conn) -> None:
    """Re-point manual pet_files rows onto whatever pet id currently carries
    the stored name, after cluster_pets has rebuilt `pets`.

    Called from pets/cluster.py before every commit/return -- unlike the
    person version this isn't defensive, it's mandatory: pet ids are
    guaranteed to change on every detect chunk (cluster_pets DELETEs and
    rebuilds `pets` wholesale each time). See
    faces.manual_tags.repair_manual_person_files for the "leave untouched if
    the name isn't currently held" and primary-key-collision handling, which
    are identical here.
    """
    rows = conn.execute("SELECT pet_id, file_id, pet_name, created_at FROM pet_files").fetchall()
    for row in rows:
        name_row = conn.execute("SELECT name FROM pets WHERE id=?", (row["pet_id"],)).fetchone()
        if name_row and name_row["name"] == row["pet_name"]:
            continue
        target = conn.execute("SELECT id FROM pets WHERE name=?", (row["pet_name"],)).fetchone()
        if not target:
            continue  # name doesn't exist right now; leave the row alone
        if target["id"] == row["pet_id"]:
            continue
        conn.execute(
            "DELETE FROM pet_files WHERE pet_id=? AND file_id=?", (target["id"], row["file_id"])
        )
        conn.execute(
            "UPDATE pet_files SET pet_id=? WHERE pet_id=? AND file_id=?",
            (target["id"], row["pet_id"], row["file_id"]),
        )
