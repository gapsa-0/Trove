"""The edit log: what it records, what it can take back, and what it survives.

The test that matters most here is the last one. `cluster_faces` DELETEs and
rebuilds every `persons` row, so an entry found by id alone would go silently
missing on the next detect chunk -- and "silently missing" is exactly the shape
of the bugs this whole batch of work is about.
"""

from trove.config import Config
from trove.db import database as db
from trove.faces import cluster as fc
from trove.services import edit_log, people_edit


def _archive(tmp_path):
    """One root, three files, two named persons with a face each."""
    db_path = tmp_path / "archive.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/photos','2026-01-01')")
    for file_id in (1, 2, 3):
        conn.execute(
            """INSERT INTO files(id,root_id,rel_path,size,mtime,media_type,
                                 first_seen,last_seen)
               VALUES(?,1,?,1,0,'image','2026-01-01','2026-01-01')""",
            (file_id, f"{file_id}.jpg"),
        )
    for pid, name, fid in ((1, "Ana", 1), (2, "Bruno", 2)):
        conn.execute(
            "INSERT INTO persons(id,name,face_count,created_at) VALUES(?,?,1,'2026-01-01')",
            (pid, name),
        )
        conn.execute(
            """INSERT INTO faces(id,file_id,box_x,box_y,box_w,box_h,det_score,
                                 embedding,person_id,quality_tier,created_at)
               VALUES(?,?,10,10,60,60,0.9,X'0000803F00000000',?,'HIGH','2026-01-01')""",
            (fid, fid, pid),
        )
    conn.commit()
    conn.close()
    return db_path


def _entries(db_path, person_id=1, name=""):
    return edit_log.entries_for(db_path, edit_log.PERSON, person_id, name)


def test_a_rename_is_recorded_and_can_be_taken_back(tmp_path):
    db_path = _archive(tmp_path)
    people_edit.rename_person(db_path, 1, "Ana María")

    entries = _entries(db_path)
    assert [e["action"] for e in entries] == ["rename"]
    assert entries[0]["detail"]["from"] == "Ana"
    assert entries[0]["detail"]["to"] == "Ana María"
    assert entries[0]["undoable"] is True

    assert edit_log.undo(db_path, entries[0]["id"]).get("ok") is True
    check = db.open_readonly(db_path)
    assert check.execute("SELECT name FROM persons WHERE id=1").fetchone()["name"] == "Ana"
    check.close()


def test_an_undone_entry_is_marked_rather_than_deleted(tmp_path):
    """A history that erased itself as you used it would be a poor account of
    what happened -- and would put the Undo button back on a done deed."""
    db_path = _archive(tmp_path)
    people_edit.rename_person(db_path, 1, "Ana María")
    first = _entries(db_path)[0]
    edit_log.undo(db_path, first["id"])

    after = {e["id"]: e for e in _entries(db_path, name="Ana")}
    assert first["id"] in after, "the entry should survive being used"
    assert after[first["id"]]["undone"] is True
    assert after[first["id"]]["undoable"] is False
    assert edit_log.undo(db_path, first["id"]) == {"error": "already undone"}


def test_a_merge_is_recorded_against_the_row_that_owns_its_undo(tmp_path):
    db_path = _archive(tmp_path)
    people_edit.merge_persons(db_path, 1, 2, name="Ana")

    entry = _entries(db_path)[0]
    assert entry["action"] == "merge"
    assert entry["detail"]["dropped_name"] == "Bruno"
    assert entry["detail"]["photos"] == 1
    assert entry["undoable"] is True

    check = db.open_readonly(db_path)
    merge_id = check.execute("SELECT id FROM person_merges").fetchone()["id"]
    check.close()
    assert people_edit.unmerge_persons(db_path, merge_id).get("ok") is True
    # unmerge_linked deletes the person_merges row; the log entry must not go
    # with it, only be marked.
    assert _entries(db_path, name="Ana")[0]["undone"] is True


def test_manual_photo_tags_are_recorded_both_ways(tmp_path):
    db_path = _archive(tmp_path)
    people_edit.add_person_to_file(db_path, 1, 3)
    people_edit.remove_person_from_file(db_path, 1, 3)
    assert [e["action"] for e in _entries(db_path)] == ["remove_photo", "add_photo"]


def test_history_survives_the_recluster_that_rebuilds_every_person_row(tmp_path):
    """cluster_faces DELETEs `persons` wholesale, so an id-only lookup loses the
    history of everyone at once. The name is the durable anchor (ADR 0008)."""
    db_path = _archive(tmp_path)
    people_edit.rename_person(db_path, 1, "Ana María")

    conn = db.connect(db_path)
    fc.cluster_faces(conn, Config())
    row = conn.execute("SELECT id FROM persons WHERE name='Ana María'").fetchone()
    conn.close()
    # Whether the rebuilt cluster kept the id is not this test's business; that
    # it can still be found by name is.
    new_id = row["id"] if row else 999
    assert [e["action"] for e in _entries(db_path, new_id, "Ana María")] == ["rename"]


def test_an_unrelated_person_does_not_inherit_an_unnamed_history(tmp_path):
    """The name fallback is guarded on non-empty, or every unnamed cluster would
    read every other unnamed cluster's edits as its own."""
    db_path = _archive(tmp_path)
    people_edit.rename_person(db_path, 1, "")
    assert _entries(db_path, 2, "") == []
