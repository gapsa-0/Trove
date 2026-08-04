from trove.db import database as db
from trove.faces.manual_tags import repair_manual_person_files
from trove.pets.manual_tags import repair_manual_pet_files
from trove.services import browse, people, people_edit, pets_edit


def _base_catalog(tmp_path):
    """A minimal archive: one root, a few files, no persons/pets/faces yet."""
    db_path = tmp_path / "archive.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/photos','2026-01-01')")
    for file_id in (1, 2, 3):
        conn.execute(
            """INSERT INTO files(
                   id,root_id,rel_path,size,mtime,media_type,first_seen,last_seen
               ) VALUES(?,1,?,1,0,'image','2026-01-01','2026-01-01')""",
            (file_id, f"{file_id}.jpg"),
        )
        conn.execute(
            "INSERT INTO dates(file_id,best_datetime) VALUES(?,'2026-01-01')",
            (file_id,),
        )
    conn.commit()
    conn.close()
    return db_path


def _add_person(conn, pid, name):
    conn.execute(
        "INSERT INTO persons(id,name,created_at) VALUES(?,?,'2026-01-01')",
        (pid, name),
    )


def _add_pet(conn, pid, name):
    conn.execute(
        "INSERT INTO pets(id,name,species,created_at) VALUES(?,?,'dog','2026-01-01')",
        (pid, name),
    )


# -- add / remove round trips ------------------------------------------------


def test_add_and_remove_person_round_trip(tmp_path):
    db_path = _base_catalog(tmp_path)
    conn = db.connect(db_path)
    _add_person(conn, 1, "Alice")
    conn.commit()
    conn.close()

    res = people_edit.add_person_to_file(db_path, 1, 1)
    assert res["ok"]
    assert res["person"] == {"id": 1, "name": "Alice"}

    check = db.open_readonly(db_path)
    row = check.execute("SELECT person_id, file_id, person_name FROM person_files").fetchone()
    assert (row["person_id"], row["file_id"], row["person_name"]) == (1, 1, "Alice")
    check.close()

    res2 = people_edit.remove_person_from_file(db_path, 1, 1)
    assert res2["ok"]
    check = db.open_readonly(db_path)
    assert check.execute("SELECT COUNT(*) FROM person_files").fetchone()[0] == 0
    check.close()


def test_add_person_unnamed_is_rejected(tmp_path):
    db_path = _base_catalog(tmp_path)
    conn = db.connect(db_path)
    _add_person(conn, 1, None)
    conn.commit()
    conn.close()

    res = people_edit.add_person_to_file(db_path, 1, 1)
    assert "error" in res
    check = db.open_readonly(db_path)
    assert check.execute("SELECT COUNT(*) FROM person_files").fetchone()[0] == 0
    check.close()


def test_add_and_remove_pet_round_trip(tmp_path):
    db_path = _base_catalog(tmp_path)
    conn = db.connect(db_path)
    _add_pet(conn, 1, "Fido")
    conn.commit()
    conn.close()

    res = pets_edit.add_pet_to_file(db_path, 1, 1)
    assert res["ok"]
    assert res["pet"] == {"id": 1, "name": "Fido"}

    res2 = pets_edit.remove_pet_from_file(db_path, 1, 1)
    assert res2["ok"]
    check = db.open_readonly(db_path)
    assert check.execute("SELECT COUNT(*) FROM pet_files").fetchone()[0] == 0
    check.close()


def test_add_pet_unnamed_is_rejected(tmp_path):
    db_path = _base_catalog(tmp_path)
    conn = db.connect(db_path)
    _add_pet(conn, 1, None)
    conn.commit()
    conn.close()

    res = pets_edit.add_pet_to_file(db_path, 1, 1)
    assert "error" in res


# -- repair: person_files survives persons being rebuilt ---------------------


def test_repair_manual_person_files_repoints_after_recluster(tmp_path):
    db_path = _base_catalog(tmp_path)
    conn = db.connect(db_path)
    _add_person(conn, 1, "Alice")
    conn.commit()
    conn.close()
    people_edit.add_person_to_file(db_path, 1, 1)

    # Simulate a recluster: the old person row is gone, a NEW id carries the
    # same name (exactly what faces/cluster.py's DELETE+rebuild does).
    conn = db.connect(db_path)
    conn.execute("DELETE FROM persons WHERE id=1")
    _add_person(conn, 2, "Alice")
    conn.commit()

    repair_manual_person_files(conn)
    conn.commit()

    row = conn.execute("SELECT person_id, person_name FROM person_files WHERE file_id=1").fetchone()
    assert row["person_id"] == 2
    assert row["person_name"] == "Alice"
    conn.close()


def test_repair_manual_person_files_leaves_untouched_when_name_gone(tmp_path):
    db_path = _base_catalog(tmp_path)
    conn = db.connect(db_path)
    _add_person(conn, 1, "Alice")
    conn.commit()
    conn.close()
    people_edit.add_person_to_file(db_path, 1, 1)

    conn = db.connect(db_path)
    conn.execute("DELETE FROM persons WHERE id=1")  # no one carries "Alice" now
    conn.commit()

    repair_manual_person_files(conn)
    conn.commit()

    row = conn.execute("SELECT person_id, person_name FROM person_files WHERE file_id=1").fetchone()
    assert row["person_id"] == 1  # untouched -- rots, but isn't deleted
    assert row["person_name"] == "Alice"
    conn.close()


def test_repair_manual_person_files_handles_pk_collision(tmp_path):
    """Two manual rows (different files) that both re-point onto the SAME
    target person must not raise the (person_id, file_id) primary key, and
    a row that's already pointed at the surviving person is left alone
    without becoming a duplicate."""
    db_path = _base_catalog(tmp_path)
    conn = db.connect(db_path)
    _add_person(conn, 1, "Alice")
    conn.commit()
    conn.close()
    people_edit.add_person_to_file(db_path, 1, 1)  # file 1 -> person 1 ("Alice")
    people_edit.add_person_to_file(db_path, 1, 2)  # file 2 -> person 1 ("Alice")

    conn = db.connect(db_path)
    conn.execute("DELETE FROM persons WHERE id=1")
    _add_person(conn, 2, "Alice")
    # file 3 is already manually pointed at the surviving id/name pair.
    conn.execute(
        "INSERT INTO person_files(person_id,file_id,person_name,created_at) "
        "VALUES(2,3,'Alice','2026-01-01')"
    )
    conn.commit()

    repair_manual_person_files(conn)  # must not raise
    conn.commit()

    rows = {
        r["file_id"]: r["person_id"]
        for r in conn.execute("SELECT file_id, person_id FROM person_files")
    }
    assert rows == {1: 2, 2: 2, 3: 2}
    conn.close()


def test_repair_manual_pet_files_repoints_after_recluster(tmp_path):
    db_path = _base_catalog(tmp_path)
    conn = db.connect(db_path)
    _add_pet(conn, 1, "Fido")
    conn.commit()
    conn.close()
    pets_edit.add_pet_to_file(db_path, 1, 1)

    conn = db.connect(db_path)
    conn.execute("DELETE FROM pets WHERE id=1")
    _add_pet(conn, 7, "Fido")
    conn.commit()

    repair_manual_pet_files(conn)
    conn.commit()

    row = conn.execute("SELECT pet_id, pet_name FROM pet_files WHERE file_id=1").fetchone()
    assert row["pet_id"] == 7
    assert row["pet_name"] == "Fido"
    conn.close()


# -- reads: manual-only files count exactly once -----------------------------


def test_face_person_manual_only_file_counted_once(tmp_path):
    db_path = _base_catalog(tmp_path)
    conn = db.connect(db_path)
    _add_person(conn, 1, "Alice")
    # A detected face of Alice on file 1.
    conn.execute(
        """INSERT INTO faces(
               id,file_id,box_x,box_y,box_w,box_h,embedding,person_id,created_at
           ) VALUES(1,1,0,0,1,1,X'00',1,'2026-01-01')"""
    )
    conn.commit()
    conn.close()

    # Manually tag Alice on file 2 (no face at all there) AND redundantly on
    # file 1 (where a face already exists) -- file 1 must still appear once.
    people_edit.add_person_to_file(db_path, 1, 2)
    people_edit.add_person_to_file(db_path, 1, 1)

    result = people.face_person(db_path, 1, root_id=1)
    assert result["photos"] == 2
    ids = sorted(item["id"] for item in result["items"])
    assert ids == [1, 2]
    by_id = {item["id"]: item for item in result["items"]}
    assert by_id[1]["face_id"] == 1
    assert by_id[2]["face_id"] is None


def test_media_person_filter_matches_manual_only_tag(tmp_path):
    db_path = _base_catalog(tmp_path)
    conn = db.connect(db_path)
    _add_person(conn, 1, "Alice")
    conn.commit()
    conn.close()

    people_edit.add_person_to_file(db_path, 1, 2)  # file 2 has no detected face

    result = browse.media(db_path, root_id=1, person_ids=[1])
    assert [item["id"] for item in result["items"]] == [2]
    assert result["total"] == 1


def test_face_persons_grid_counts_include_manual_only_files(tmp_path):
    db_path = _base_catalog(tmp_path)
    conn = db.connect(db_path)
    _add_person(conn, 1, "Alice")
    conn.execute(
        """INSERT INTO faces(
               id,file_id,box_x,box_y,box_w,box_h,embedding,person_id,created_at
           ) VALUES(1,1,0,0,1,1,X'00',1,'2026-01-01')"""
    )
    conn.commit()
    conn.close()

    people_edit.add_person_to_file(db_path, 1, 2)

    result = people.face_persons(db_path, root_id=1)
    person = next(p for p in result["people"] if p["id"] == 1)
    assert person["photos"] == 2
