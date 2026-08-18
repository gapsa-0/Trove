"""Naming somebody from the photograph rather than from the People screen.

The panel beside a photo could only ever point a face at somebody *already*
named, so on an archive where nobody is named it had nothing to offer but a
sentence sending you to another screen -- and the face in front of you is the
moment you actually know who somebody is.

What "name this face" means depends on what the face already is, and the three
answers are what these pin down. Every one of them also has to leave a pin
behind (``faces.manual_person``), because the ``persons`` table is deleted and
rebuilt by every clustering pass and a name held nowhere else does not survive
it -- which is checked by running a real re-cluster over the result.
"""

import factories

from trove.config import Config
from trove.db import database as db
from trove.faces import cluster as fc
from trove.services import people_edit


def _archive(tmp_path):
    """Three files, and nothing named."""
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
    return db_path, conn


def _add_face(conn, face_id, file_id, person_id=None):
    factories.add_face(conn, file_id, face_id=face_id, person_id=person_id)


def test_naming_a_face_in_an_unnamed_group_names_the_group(tmp_path):
    """The same act as typing into that group's card: the face you pointed at is
    one of theirs, and so are the rest."""
    db_path, conn = _archive(tmp_path)
    conn.execute("INSERT INTO persons(id,name,face_count,created_at) VALUES(7,NULL,2,'2026-01-01')")
    _add_face(conn, 1, 1, person_id=7)
    _add_face(conn, 2, 2, person_id=7)
    conn.commit()
    conn.close()

    res = people_edit.name_face(str(db_path), 1, "Ana")
    assert res["ok"] is True
    assert res["person"]["id"] == 7, "a new person was made instead of naming the one it was in"

    conn = db.connect(db_path)
    assert conn.execute("SELECT name FROM persons WHERE id=7").fetchone()["name"] == "Ana"
    # Both faces are still hers; naming a group does not take the group apart.
    assert conn.execute("SELECT COUNT(*) FROM faces WHERE person_id=7").fetchone()[0] == 2
    conn.close()


def test_naming_a_face_a_name_already_taken_moves_it_to_that_person(tmp_path):
    """Typing a name somebody already carries says who this is, not that there
    are two of them."""
    db_path, conn = _archive(tmp_path)
    conn.execute(
        "INSERT INTO persons(id,name,face_count,created_at) VALUES(7,'Ana',1,'2026-01-01')"
    )
    conn.execute("INSERT INTO persons(id,name,face_count,created_at) VALUES(8,NULL,1,'2026-01-01')")
    _add_face(conn, 1, 1, person_id=7)
    _add_face(conn, 2, 2, person_id=8)
    conn.commit()
    conn.close()

    # Deliberately in the other case: a name is a name, not a string to match.
    res = people_edit.name_face(str(db_path), 2, "ana")
    assert res["person"]["id"] == 7
    assert res["person"]["name"] == "Ana", "the spelling already on file is the one kept"

    conn = db.connect(db_path)
    assert conn.execute("SELECT person_id FROM faces WHERE id=2").fetchone()["person_id"] == 7
    assert conn.execute("SELECT COUNT(*) FROM persons WHERE name='Ana'").fetchone()[0] == 1
    conn.close()


def test_naming_a_face_that_is_in_no_group_makes_one_for_it(tmp_path):
    """A group needs `faces_min_faces` faces to exist at all, so plenty of faces
    belong to nobody -- and those are exactly the ones the old panel could say
    nothing about."""
    db_path, conn = _archive(tmp_path)
    _add_face(conn, 1, 1)
    conn.commit()
    conn.close()

    res = people_edit.name_face(str(db_path), 1, "Ana")
    assert res["ok"] is True

    conn = db.connect(db_path)
    row = conn.execute("SELECT person_id, manual_person FROM faces WHERE id=1").fetchone()
    assert row["person_id"] == res["person"]["id"]
    assert row["manual_person"] == "Ana"
    conn.close()


def test_a_name_given_from_a_photo_survives_the_next_clustering_pass(tmp_path):
    """The point of the pin, and the only version of this worth having: every
    clustering pass DELETEs the persons table and rebuilds it."""
    db_path, conn = _archive(tmp_path)
    _add_face(conn, 1, 1)
    conn.commit()
    conn.close()

    people_edit.name_face(str(db_path), 1, "Ana")

    conn = db.connect(db_path)
    fc.cluster_faces(conn, Config.load())
    named = conn.execute("SELECT id, name FROM persons WHERE name='Ana'").fetchall()
    assert len(named) == 1, "the name did not survive the rebuild"
    assert (
        conn.execute("SELECT person_id FROM faces WHERE id=1").fetchone()["person_id"]
        == named[0]["id"]
    )
    conn.close()


def test_naming_a_face_refuses_an_empty_name(tmp_path):
    """Nothing is not a name, and the caller that sends one has a bug rather
    than an intention."""
    db_path, conn = _archive(tmp_path)
    _add_face(conn, 1, 1)
    conn.commit()
    conn.close()

    assert "error" in people_edit.name_face(str(db_path), 1, "   ")
    assert "error" in people_edit.name_face(str(db_path), None, "Ana")
    assert "error" in people_edit.name_face(str(db_path), 999, "Ana")
