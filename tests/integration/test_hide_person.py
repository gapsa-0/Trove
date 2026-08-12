"""Hiding a cluster, for the two quite different reasons one gets hidden.

The distinction is the whole design. "Not a person" is a claim about the
detections and takes them out of clustering for good. "Unknown" is a claim
about the list -- a real person you would rather not see on it -- and must
leave the faces alone, or hiding your neighbour would tell the clusterer that
their face is a doll's.

The test that would catch the tempting shortcut is
``test_a_hidden_person_is_still_hidden_after_a_recluster``: storing the flag
only on the persons row works perfectly until the next detect chunk, which
deletes every persons row and rebuilds it.
"""

from trove.config import Config
from trove.db import database as db
from trove.faces import cluster as fc
from trove.services import people, people_edit


def _archive(tmp_path):
    """Two people, three faces each.

    Three because `faces_min_faces` is 3: a smaller group is dissolved by
    clustering rather than kept, and half these tests run a real recluster.
    """
    db_path = tmp_path / "archive.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/photos','2026-01-01')")
    for file_id in range(1, 7):
        conn.execute(
            """INSERT INTO files(id,root_id,rel_path,size,mtime,media_type,
                                 first_seen,last_seen)
               VALUES(?,1,?,1,0,'image','2026-01-01','2026-01-01')""",
            (file_id, f"{file_id}.jpg"),
        )
    # Two well-separated directions, so a recluster keeps them apart.
    faces = {1: ((1, 2, 3), "X'0000803F00000000'"), 2: ((4, 5, 6), "X'000000000000803F'")}
    for pid, (face_ids, vector) in faces.items():
        conn.execute(
            "INSERT INTO persons(id,name,face_count,created_at) VALUES(?,?,3,'2026-01-01')",
            (pid, f"P{pid}"),
        )
        for face_id in face_ids:
            conn.execute(
                f"""INSERT INTO faces(id,file_id,box_x,box_y,box_w,box_h,det_score,
                                      embedding,person_id,quality_tier,created_at)
                    VALUES(?,?,10,10,60,60,0.9,{vector},?,'HIGH','2026-01-01')""",
                (face_id, face_id, pid),
            )
    conn.commit()
    conn.close()
    return db_path


def _row(db_path, person_id):
    check = db.open_readonly(db_path)
    row = check.execute("SELECT * FROM persons WHERE id=?", (person_id,)).fetchone()
    faces = check.execute("SELECT person_id, not_person FROM faces WHERE id IN (1,2)").fetchall()
    check.close()
    return row, [(f["person_id"], f["not_person"]) for f in faces]


def test_hiding_an_unknown_person_leaves_their_faces_alone(tmp_path):
    db_path = _archive(tmp_path)
    assert people_edit.hide_person(db_path, 1, reason="unknown").get("ok") is True

    row, faces = _row(db_path, 1)
    assert row["hidden"] == 1
    assert row["name"] == "P1", "hiding is not forgetting who they are"
    assert faces == [(1, 0), (1, 0)], "an unknown person's faces keep clustering"


def test_a_hidden_person_leaves_the_grid_but_can_still_be_listed(tmp_path):
    db_path = _archive(tmp_path)
    people_edit.hide_person(db_path, 1, reason="unknown")

    visible = people.face_persons(db_path, 1)["people"]
    assert [p["id"] for p in visible] == [2]
    hidden = people.face_persons(db_path, 1, hidden=True)["people"]
    assert [p["id"] for p in hidden] == [1]


def test_the_counts_follow_the_grid(tmp_path):
    db_path = _archive(tmp_path)
    before = people.face_summary(db_path, 1, 5)
    people_edit.hide_person(db_path, 1, reason="unknown")
    after = people.face_summary(db_path, 1, 5)

    assert after["people"] == before["people"] - 1
    assert after["hidden_people"] == 1
    assert after["faces"] == before["faces"], "the faces are still there to be counted"


def test_a_hidden_person_is_not_offered_for_merging(tmp_path):
    """Asking "is this hidden person the same as that one?" is a question about
    somebody who is deliberately not on the screen."""
    db_path = _archive(tmp_path)
    conn = db.connect(db_path)
    for pid in (1, 2):
        conn.execute("UPDATE persons SET centroid=X'0000803F00000000' WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    assert people.person_suggestions(db_path, 1)["suggestions"], "expected a pair before hiding"

    people_edit.hide_person(db_path, 1, reason="unknown")
    assert people.person_suggestions(db_path, 1)["suggestions"] == []


def test_a_hidden_person_is_still_hidden_after_a_recluster(tmp_path):
    """The regression that a persons-row flag alone would not survive."""
    db_path = _archive(tmp_path)
    people_edit.hide_person(db_path, 1, reason="unknown")

    conn = db.connect(db_path)
    fc.cluster_faces(conn, Config())
    conn.close()

    check = db.open_readonly(db_path)
    still = check.execute(
        """SELECT p.hidden FROM persons p JOIN faces fa ON fa.person_id=p.id
           WHERE fa.id=1"""
    ).fetchone()
    check.close()
    assert still and still["hidden"] == 1, "the recluster put a hidden person back on the screen"


def test_unhiding_puts_them_back_and_does_not_come_undone(tmp_path):
    db_path = _archive(tmp_path)
    people_edit.hide_person(db_path, 1, reason="unknown")
    assert people_edit.unhide_person(db_path, 1).get("ok") is True

    assert [p["id"] for p in people.face_persons(db_path, 1)["people"]] == [1, 2]
    conn = db.connect(db_path)
    fc.cluster_faces(conn, Config())
    conn.close()
    # The record was dropped too, so the recluster must not re-hide them.
    visible = {p["id"] for p in people.face_persons(db_path, 1)["people"]}
    assert visible, "unhiding was undone by the next clustering pass"


def test_not_a_person_still_behaves_exactly_as_it_did(tmp_path):
    """A characterisation test: this path is not what changed, and must not."""
    db_path = _archive(tmp_path)
    assert people_edit.hide_person(db_path, 1, kind="toy").get("ok") is True

    check = db.open_readonly(db_path)
    assert check.execute("SELECT 1 FROM persons WHERE id=1").fetchone() is None
    faces = check.execute("SELECT person_id, not_person, nonhuman_kind FROM faces WHERE id=1")
    row = faces.fetchone()
    check.close()
    assert (row["person_id"], row["not_person"], row["nonhuman_kind"]) == (None, 1, "toy")
