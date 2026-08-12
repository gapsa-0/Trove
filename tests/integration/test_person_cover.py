"""Choosing which photo represents a person, and having the choice stick.

`persons.cover_face_id` is re-derived constantly -- after any edit that moves a
face (`_sync_person_stats`) and from scratch on every recluster
(`_write_people`, `_refresh_person_stats`). A choice recorded only there is
correct until the next of those and then silently reverts to the sharpest face,
which is the hardest kind of bug to notice by hand.

So the two tests that matter are the two that do something else entirely first
and then look at the cover again.
"""

from trove.config import Config
from trove.db import database as db
from trove.faces import cluster as fc
from trove.services import people_edit


def _archive(tmp_path):
    """One person, three faces, deliberately of differing sharpness.

    Face 1 is the sharpest, so it is what every automatic rule picks; the tests
    choose face 3 precisely because nothing would choose it on its own.
    """
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
    conn.execute(
        "INSERT INTO persons(id,name,face_count,cover_face_id,created_at) "
        "VALUES(1,'Ana',3,1,'2026-01-01')"
    )
    for face_id, det in ((1, 0.99), (2, 0.90), (3, 0.80)):
        conn.execute(
            """INSERT INTO faces(id,file_id,box_x,box_y,box_w,box_h,det_score,
                                 embedding,person_id,quality_tier,created_at)
               VALUES(?,?,10,10,60,60,?,X'0000803F00000000',1,'HIGH','2026-01-01')""",
            (face_id, face_id, det),
        )
    conn.commit()
    conn.close()
    return db_path


def _cover(db_path):
    check = db.open_readonly(db_path)
    row = check.execute("SELECT cover_face_id FROM persons WHERE name='Ana'").fetchone()
    check.close()
    return row["cover_face_id"] if row else None


def test_choosing_a_cover_sets_it_and_clears_the_previous_one(tmp_path):
    db_path = _archive(tmp_path)
    assert people_edit.set_person_cover(db_path, 1, 3).get("ok") is True
    assert _cover(db_path) == 3

    people_edit.set_person_cover(db_path, 1, 2)
    check = db.open_readonly(db_path)
    pinned = [r["id"] for r in check.execute("SELECT id FROM faces WHERE manual_cover=1")]
    check.close()
    assert pinned == [2], "only one face can be the cover"


def test_the_chosen_cover_survives_an_unrelated_edit(tmp_path):
    """Anything that moves a face re-derives the cover through _sync_person_stats."""
    db_path = _archive(tmp_path)
    people_edit.set_person_cover(db_path, 1, 3)

    people_edit.detach_file_from_person(db_path, 1, 2)
    assert _cover(db_path) == 3, "an edit elsewhere reverted the chosen cover"


def test_the_chosen_cover_survives_a_recluster(tmp_path):
    """The rebuild deletes every persons row, so the pin has to be on the face."""
    db_path = _archive(tmp_path)
    people_edit.set_person_cover(db_path, 1, 3)

    conn = db.connect(db_path)
    fc.cluster_faces(conn, Config())
    conn.close()
    assert _cover(db_path) == 3, "the recluster went back to the sharpest face"


def test_a_face_that_is_not_theirs_is_refused(tmp_path):
    db_path = _archive(tmp_path)
    conn = db.connect(db_path)
    conn.execute(
        """INSERT INTO faces(id,file_id,box_x,box_y,box_w,box_h,det_score,
                             embedding,quality_tier,created_at)
           VALUES(9,1,10,10,60,60,0.9,X'0000803F00000000','HIGH','2026-01-01')"""
    )
    conn.commit()
    conn.close()
    assert "error" in people_edit.set_person_cover(db_path, 1, 9)
    assert _cover(db_path) == 1


def test_a_face_the_quality_gate_hides_cannot_be_the_cover(tmp_path):
    """A cover nobody can see anywhere else in the app is not a cover."""
    db_path = _archive(tmp_path)
    conn = db.connect(db_path)
    conn.execute("UPDATE faces SET quality_tier='LOW_QUALITY' WHERE id=3")
    conn.commit()
    conn.close()
    assert "error" in people_edit.set_person_cover(db_path, 1, 3)
