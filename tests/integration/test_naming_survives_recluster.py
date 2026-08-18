"""A name given to a group, and whether the next clustering pass keeps it.

`persons` is DELETEd and rebuilt from scratch every time clustering runs, so a
name is only ever as durable as whatever carries it across that. Two things can:
`_carry_names`, which hands each old name to the one new cluster that best
inherits its faces, and a `faces.manual_person` pin, which is an instruction the
rebuild obeys.

The first is a guess and can come up empty -- it needs three faces of overlap,
and a group whose faces no longer cluster together at all offers none. Which is
exactly the group most likely to have been named by hand in the first place.
"""

import factories

from trove.config import Config
from trove.db import database as db
from trove.faces import cluster as fc
from trove.services import people_edit


def _person_too_small_to_recluster(tmp_path):
    """A named person with two faces, where clustering needs three.

    Not a contrived case: `faces_min_faces` is 3, so a person you named from a
    couple of photographs has nothing for the rebuild to re-form them into.
    """
    db_path = tmp_path / "archive.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/photos','2026-01-01')")
    person_id = factories.add_person(conn, name=None)
    for file_id in (1, 2):
        conn.execute(
            """INSERT INTO files(id,root_id,rel_path,size,mtime,media_type,
                                 first_seen,last_seen)
               VALUES(?,1,?,1,0,'image','2026-01-01','2026-01-01')""",
            (file_id, f"{file_id}.jpg"),
        )
        factories.add_face(conn, file_id, person_id=person_id)
    conn.execute("UPDATE persons SET face_count=2 WHERE id=?", (person_id,))
    conn.commit()
    conn.close()
    return db_path, person_id


def test_a_name_survives_a_pass_that_cannot_reform_the_group(tmp_path):
    """The name is the one thing in a person that came from the user, and it was
    the first thing a rebuild dropped."""
    db_path, person_id = _person_too_small_to_recluster(tmp_path)

    assert people_edit.rename_person(str(db_path), person_id, "Mari")["ok"] is True

    conn = db.connect(db_path)
    fc.cluster_faces(conn, Config.load())
    named = conn.execute("SELECT id FROM persons WHERE name='Mari'").fetchall()
    conn.close()
    assert len(named) == 1, "the name did not survive the rebuild"


def test_naming_pins_one_face_rather_than_the_whole_group(tmp_path):
    """A pin is an instruction, not a note: every pinned face is forced into the
    person carrying its name whatever the embeddings say. Pinning all of them
    would make a named person un-splittable for good, which is the opposite of
    what naming should cost -- so exactly one face anchors the name.
    """
    db_path, person_id = _person_too_small_to_recluster(tmp_path)

    people_edit.rename_person(str(db_path), person_id, "Mari")

    conn = db.connect(db_path)
    pinned = conn.execute("SELECT COUNT(*) FROM faces WHERE manual_person='Mari'").fetchone()[0]
    conn.close()
    assert pinned == 1, f"{pinned} faces pinned; the anchor should be one"


def test_taking_the_name_off_releases_the_anchor_too(tmp_path):
    """...or the pin outlives the name and the next pass rebuilds the person
    under the name just removed, which is the bug test_unnaming.py is about."""
    db_path, person_id = _person_too_small_to_recluster(tmp_path)

    people_edit.rename_person(str(db_path), person_id, "Mari")
    people_edit.rename_person(str(db_path), person_id, "")

    conn = db.connect(db_path)
    assert (
        conn.execute("SELECT COUNT(*) FROM faces WHERE manual_person IS NOT NULL").fetchone()[0]
        == 0
    )
    fc.cluster_faces(conn, Config.load())
    assert conn.execute("SELECT COUNT(*) FROM persons WHERE name='Mari'").fetchone()[0] == 0
    conn.close()
