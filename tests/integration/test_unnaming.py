"""Taking a name off a person, and having it stay off.

Clearing the name always worked at every layer -- the service writes NULL, no
validation refuses it. What did not work was staying cleared: a face pinned to
that person still carried the name in `faces.manual_person`, and the next
recluster's `_apply_manual_pins` reads exactly that column, finds no person
with the name any more, and creates one. So the person came back, under the
name you had just removed, with a new id.

The pin is what makes "move this face to Mari" survive a rebuild, so it cannot
simply be ignored; releasing it is part of what un-naming means.
"""

from trove.config import Config
from trove.db import database as db
from trove.faces import cluster as fc
from trove.services import edit_log, people_edit


def _archive(tmp_path, *, pinned: bool = True):
    """One person, "Mari", with two faces -- one of them manually pinned."""
    db_path = tmp_path / "archive.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/photos','2026-01-01')")
    for file_id in (1, 2):
        conn.execute(
            """INSERT INTO files(id,root_id,rel_path,size,mtime,media_type,
                                 first_seen,last_seen)
               VALUES(?,1,?,1,0,'image','2026-01-01','2026-01-01')""",
            (file_id, f"{file_id}.jpg"),
        )
    conn.execute(
        "INSERT INTO persons(id,name,face_count,created_at) VALUES(1,'Mari',2,'2026-01-01')"
    )
    for face_id in (1, 2):
        conn.execute(
            """INSERT INTO faces(id,file_id,box_x,box_y,box_w,box_h,det_score,
                                 embedding,person_id,quality_tier,manual_person,created_at)
               VALUES(?,?,10,10,60,60,0.9,X'0000803F00000000',1,'HIGH',?,'2026-01-01')""",
            (face_id, face_id, "Mari" if (pinned and face_id == 1) else None),
        )
    conn.commit()
    conn.close()
    return db_path


def _names(db_path):
    check = db.open_readonly(db_path)
    names = {r["name"] for r in check.execute("SELECT name FROM persons")}
    pins = {r["manual_person"] for r in check.execute("SELECT manual_person FROM faces")}
    check.close()
    return names, pins


def test_a_cleared_name_does_not_come_back_at_the_next_recluster(tmp_path):
    """The regression this file exists for."""
    db_path = _archive(tmp_path)
    assert people_edit.rename_person(db_path, 1, "").get("ok") is True

    conn = db.connect(db_path)
    fc.cluster_faces(conn, Config())
    conn.close()

    names, pins = _names(db_path)
    assert "Mari" not in names, "the name came back as a new person on the recluster"
    assert pins == {None}, "a face still pinned to the old name will recreate it"


def test_clearing_a_name_releases_only_that_persons_pins(tmp_path):
    """Someone else's pin is not this person's to drop."""
    db_path = _archive(tmp_path)
    conn = db.connect(db_path)
    conn.execute("INSERT INTO persons(id,name,face_count,created_at) VALUES(2,'Bo',0,'2026-01-01')")
    conn.execute(
        """INSERT INTO faces(id,file_id,box_x,box_y,box_w,box_h,det_score,embedding,
                             person_id,quality_tier,manual_person,created_at)
           VALUES(3,2,10,10,60,60,0.9,X'0000803F00000000',2,'HIGH','Bo','2026-01-01')"""
    )
    conn.commit()
    conn.close()

    people_edit.rename_person(db_path, 1, "")
    _names_now, pins = _names(db_path)
    assert "Bo" in pins


def test_renaming_to_a_different_name_still_carries_the_pins(tmp_path):
    """The release must not over-reach into the ordinary rename it lives beside."""
    db_path = _archive(tmp_path)
    people_edit.rename_person(db_path, 1, "Mari Elena")
    names, pins = _names(db_path)
    assert "Mari Elena" in names
    assert "Mari Elena" in pins, "a rename must move the pin, not drop it"


def test_undoing_an_un_naming_restores_the_name_and_its_pins(tmp_path):
    """Undo is only honest if the face goes back to being pinned too; otherwise
    the name returns and the next recluster scatters the faces it named."""
    db_path = _archive(tmp_path)
    people_edit.rename_person(db_path, 1, "")
    entry = edit_log.entries_for(db_path, edit_log.PERSON, 1)[0]
    assert entry["action"] == "rename"

    assert edit_log.undo(db_path, entry["id"]).get("ok") is True
    names, pins = _names(db_path)
    assert "Mari" in names
    assert "Mari" in pins


def test_a_person_with_no_pins_is_simply_un_named(tmp_path):
    """The ordinary case still behaves, and touches nothing it need not."""
    db_path = _archive(tmp_path, pinned=False)
    people_edit.rename_person(db_path, 1, "")
    names, pins = _names(db_path)
    assert names == {None}
    assert pins == {None}
