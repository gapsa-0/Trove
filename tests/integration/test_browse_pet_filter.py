"""Filtering the Browse grid by pet, the way it already filters by person.

The two are the same question asked of a different table, so these mirror
``test_manual_tags``'s person-filter tests -- including the one that matters
most in both cases: a photo tagged by hand has no detection in it, and a filter
that only looked at detections would silently drop it.
"""

from trove.db import database as db
from trove.services import browse, pets_edit


def _archive(tmp_path):
    """Three files: one with Rocco detected, one with Nala, one with neither."""
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
    for pet_id, name, species in ((1, "Rocco", "dog"), (2, "Nala", "cat"), (3, None, "dog")):
        conn.execute(
            "INSERT INTO pets(id,name,species,detection_count,created_at) "
            "VALUES(?,?,?,1,'2026-01-01')",
            (pet_id, name, species),
        )
    for det_id, file_id, pet_id in ((1, 1, 1), (2, 2, 2), (3, 3, 3)):
        conn.execute(
            """INSERT INTO animal_detections(id,file_id,species,box_x,box_y,box_w,box_h,
                                             det_score,pet_id,model_source,created_at)
               VALUES(?,?,'dog',0,0,10,10,0.9,?,'test','2026-01-01')""",
            (det_id, file_id, pet_id),
        )
    conn.commit()
    conn.close()
    return db_path


def test_the_grid_can_be_narrowed_to_one_pet(tmp_path):
    db_path = _archive(tmp_path)
    result = browse.media(db_path, root_id=1, pet_ids=[1])
    assert [item["id"] for item in result["items"]] == [1]
    assert result["total"] == 1


def test_selecting_two_pets_asks_for_both_in_one_photo(tmp_path):
    """Same rule the people filter states in its own help text, so the two
    controls cannot mean different things by the same gesture."""
    db_path = _archive(tmp_path)
    assert browse.media(db_path, root_id=1, pet_ids=[1, 2])["items"] == []

    conn = db.connect(db_path)
    conn.execute(
        """INSERT INTO animal_detections(id,file_id,species,box_x,box_y,box_w,box_h,
                                         det_score,pet_id,model_source,created_at)
           VALUES(9,1,'cat',0,0,10,10,0.9,2,'test','2026-01-01')"""
    )
    conn.commit()
    conn.close()
    assert [i["id"] for i in browse.media(db_path, root_id=1, pet_ids=[1, 2])["items"]] == [1]


def test_a_hand_tagged_photo_matches_too(tmp_path):
    """It has no detection in it, so a filter reading only animal_detections
    would leave it out of its own pet's results."""
    db_path = _archive(tmp_path)
    pets_edit.add_pet_to_file(db_path, 1, 3)
    ids = [item["id"] for item in browse.media(db_path, root_id=1, pet_ids=[1])["items"]]
    assert ids == [1, 3]


def test_only_named_pets_are_offered_as_filters(tmp_path):
    """An unnamed group has no name to put in the list."""
    db_path = _archive(tmp_path)
    offered = browse.browse_filters(db_path, 1)["pets"]
    assert [p["name"] for p in offered] == ["Nala", "Rocco"]


def test_the_pet_filter_composes_with_the_others(tmp_path):
    db_path = _archive(tmp_path)
    both = browse.media(db_path, root_id=1, pet_ids=[1], mtype="image")
    assert [item["id"] for item in both["items"]] == [1]
    assert browse.media(db_path, root_id=1, pet_ids=[1], mtype="video")["items"] == []
