"""A pet's card and its photos, brought level with a person's.

Two claims, and both are about surviving something rather than about the write
itself. `cluster_pets` DELETEs and rebuilds every `pets` row after each detect
chunk, so a chosen cover recorded on that row lasts until the next chunk, and a
detached photo comes straight back unless something durable says otherwise.
Those are the tests worth having; the writes are the easy half.
"""

from trove.config import Config
from trove.db import database as db
from trove.pets import cluster as pc
from trove.services import pets, pets_edit


def _archive(tmp_path):
    """One pet, three detections of differing confidence, on three files."""
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
        "INSERT INTO pets(id,name,species,cover_detection_id,detection_count,created_at) "
        "VALUES(1,'Rocco','dog',1,3,'2026-01-01')"
    )
    for det_id, score in ((1, 0.99), (2, 0.90), (3, 0.80)):
        conn.execute(
            """INSERT INTO animal_detections(id,file_id,species,box_x,box_y,box_w,box_h,
                                             det_score,embedding,pet_id,model_source,created_at)
               VALUES(?,?,'dog',10,10,60,60,?,X'0000803F00000000',1,'test','2026-01-01')""",
            (det_id, det_id, score),
        )
    conn.commit()
    conn.close()
    return db_path


def _cover(db_path):
    check = db.open_readonly(db_path)
    row = check.execute("SELECT cover_detection_id FROM pets WHERE id=1").fetchone()
    check.close()
    return row["cover_detection_id"] if row else None


def test_a_pets_card_offers_several_photos_not_one(tmp_path):
    """The card drew a single thumbnail, which said little about a group of
    twenty photos. It is a collage now, as a person's has always been."""
    db_path = _archive(tmp_path)
    group = pets.pet_groups(db_path, 1)["pets"][0]
    assert group["detections_preview"] == [1, 2, 3]


def test_the_chosen_cover_leads_the_collage_and_the_card(tmp_path):
    db_path = _archive(tmp_path)
    assert pets_edit.set_pet_cover(db_path, 1, 3).get("ok") is True

    group = pets.pet_groups(db_path, 1)["pets"][0]
    assert group["cover_detection_id"] == 3
    assert group["detections_preview"][0] == 3, "the card should open on the chosen photo"


def test_the_chosen_cover_survives_a_recluster(tmp_path):
    db_path = _archive(tmp_path)
    pets_edit.set_pet_cover(db_path, 1, 3)

    conn = db.connect(db_path)
    pc.cluster_pets(conn, Config())
    conn.close()

    check = db.open_readonly(db_path)
    row = check.execute(
        "SELECT p.cover_detection_id FROM pets p JOIN animal_detections a ON a.pet_id=p.id"
        " WHERE a.id=3"
    ).fetchone()
    check.close()
    assert row and row["cover_detection_id"] == 3, "the rebuild went back to the best score"


def test_removing_a_photo_takes_it_out_and_records_why(tmp_path):
    """What this actually guarantees, which is less than it sounds.

    The photo leaves the group now, and a cannot-link is written so the merge
    graph will not put the two back together. It does NOT survive a rebuild:
    pet_links only blocks groups being merged and never splits one the
    automatic pass formed on its own (see pets/cluster.py::_apply_links), so a
    later clustering pass can regroup the same embedding. The People side's
    "This is not the person" has exactly the same limitation, by the same
    mechanism -- naming it here so the next reader does not assume otherwise.
    """
    db_path = _archive(tmp_path)
    assert pets_edit.detach_file_from_pet(db_path, 1, 3).get("ok") is True

    check = db.open_readonly(db_path)
    assert check.execute("SELECT pet_id FROM animal_detections WHERE id=3").fetchone()[0] is None
    links = check.execute("SELECT kind FROM pet_links").fetchall()
    check.close()
    assert [r["kind"] for r in links] == ["different"], "nothing blocks it coming back"


def test_removing_a_photo_that_is_not_theirs_says_so(tmp_path):
    db_path = _archive(tmp_path)
    assert "error" in pets_edit.detach_file_from_pet(db_path, 1, 99)


def test_a_pet_whose_last_photo_is_removed_stops_being_a_pet(tmp_path):
    """There is nothing left to recompute from -- not even a species, which the
    schema requires -- so the group goes, as an emptied person's does."""
    db_path = _archive(tmp_path)
    for file_id in (1, 2, 3):
        pets_edit.detach_file_from_pet(db_path, 1, file_id)

    check = db.open_readonly(db_path)
    assert check.execute("SELECT 1 FROM pets WHERE id=1").fetchone() is None
    check.close()


def test_a_cover_that_is_not_theirs_is_refused(tmp_path):
    db_path = _archive(tmp_path)
    conn = db.connect(db_path)
    conn.execute(
        """INSERT INTO animal_detections(id,file_id,species,box_x,box_y,box_w,box_h,
                                         det_score,embedding,model_source,created_at)
           VALUES(9,1,'cat',10,10,60,60,0.9,X'0000803F00000000','test','2026-01-01')"""
    )
    conn.commit()
    conn.close()
    assert "error" in pets_edit.set_pet_cover(db_path, 1, 9)
    assert _cover(db_path) == 1


def test_hiding_a_pet_as_unknown_leaves_its_detections_alone(tmp_path):
    """ "Unknown animal" is about the list; the animal is still an animal."""
    db_path = _archive(tmp_path)
    assert pets_edit.hide_pet(db_path, 1, reason="unknown").get("ok") is True

    check = db.open_readonly(db_path)
    row = check.execute("SELECT hidden, name FROM pets WHERE id=1").fetchone()
    assigned = check.execute("SELECT COUNT(*) FROM animal_detections WHERE pet_id=1").fetchone()[0]
    check.close()
    assert (row["hidden"], row["name"]) == (1, "Rocco")
    assert assigned == 3, "hiding a group must not unassign its photos"
    assert [p["id"] for p in pets.pet_groups(db_path, 1)["pets"]] == []
    assert [p["id"] for p in pets.pet_groups(db_path, 1, hidden=True)["pets"]] == [1]


def test_a_hidden_pet_is_still_hidden_after_a_recluster(tmp_path):
    """pets.hidden alone cannot hold it: cluster_pets deletes every pets row."""
    db_path = _archive(tmp_path)
    pets_edit.hide_pet(db_path, 1, reason="unknown")

    conn = db.connect(db_path)
    pc.cluster_pets(conn, Config())
    conn.close()

    check = db.open_readonly(db_path)
    still = check.execute(
        "SELECT p.hidden FROM pets p JOIN animal_detections a ON a.pet_id=p.id WHERE a.id=1"
    ).fetchone()
    check.close()
    assert still and still["hidden"] == 1, "the recluster put a hidden group back on the screen"


def test_not_an_animal_takes_the_photos_out_of_grouping(tmp_path):
    """A soft toy is not an animal, and must not be regrouped into one."""
    db_path = _archive(tmp_path)
    assert pets_edit.hide_pet(db_path, 1, reason="not_animal").get("ok") is True

    conn = db.connect(db_path)
    pc.cluster_pets(conn, Config())
    conn.close()

    check = db.open_readonly(db_path)
    regrouped = check.execute(
        "SELECT COUNT(*) FROM animal_detections WHERE pet_id IS NOT NULL"
    ).fetchone()[0]
    check.close()
    assert regrouped == 0, "the next pass rebuilt the group it was told was not an animal"


def test_unhiding_puts_a_pet_back(tmp_path):
    db_path = _archive(tmp_path)
    pets_edit.hide_pet(db_path, 1, reason="unknown")
    assert pets_edit.unhide_pet(db_path, 1).get("ok") is True
    assert [p["id"] for p in pets.pet_groups(db_path, 1)["pets"]] == [1]


def test_the_pet_counts_follow_the_grid(tmp_path):
    db_path = _archive(tmp_path)
    before = pets.pet_summary(db_path, 1, 5)
    pets_edit.hide_pet(db_path, 1, reason="unknown")
    after = pets.pet_summary(db_path, 1, 5)
    assert after["pets"] == before["pets"] - 1
    assert after["hidden_pets"] == 1
