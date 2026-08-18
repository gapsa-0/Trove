"""Which rebuilt group inherits a pet's name.

`pets` is deleted and rebuilt in full after every detection chunk, so a name
survives only by being carried onto one of the new groups. Whichever group best
explains the old one is the right answer -- but "best" has to mean best among
all of them, once, or one name lands on several groups at the same time and the
screen shows two cats called Kira.

People solved this first (`faces/cluster.py::_carry_names`): sort every
(name, group) overlap descending, assign greedily, each name used once and each
group named once, with a floor that rejects an overlap too small to mean
anything. This is that rule for pets.
"""

import factories
import numpy as np

from trove.config import Config
from trove.db import database as db
from trove.pets import cluster as pc


def _vec(*values):
    v = np.array(values, dtype="float32")
    return (v / np.linalg.norm(v)).tobytes()


def _two_groups_from_one_named_pet(tmp_path):
    """One named pet whose detections no longer look alike.

    Four detections: two pointing one way and two the other, far enough apart
    that clustering makes two groups of them. Only one of the two can honestly
    be Kira.
    """
    db_path = tmp_path / "archive.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/photos','2026-01-01')")
    pet_id = factories.add_pet(conn, name="Kira")
    for file_id, embedding in (
        (1, _vec(1, 0, 0)),
        (2, _vec(1, 0.02, 0)),
        (3, _vec(0, 1, 0)),
        (4, _vec(0, 1, 0.02)),
    ):
        conn.execute(
            """INSERT INTO files(id,root_id,rel_path,size,mtime,media_type,
                                 first_seen,last_seen)
               VALUES(?,1,?,1,0,'image','2026-01-01','2026-01-01')""",
            (file_id, f"{file_id}.jpg"),
        )
        factories.add_animal_detection(conn, file_id, pet_id=pet_id, embedding=embedding)
    conn.execute("UPDATE pets SET detection_count=4 WHERE id=?", (pet_id,))
    conn.commit()
    return db_path, conn


def test_a_name_lands_on_one_rebuilt_group_not_on_several(tmp_path):
    """Both halves overlap the old group equally well, and only one of them is
    Kira -- a screen with two cats of that name is worse than one unnamed."""
    _, conn = _two_groups_from_one_named_pet(tmp_path)

    pc.cluster_pets(conn, Config.load())

    named = conn.execute("SELECT id FROM pets WHERE name='Kira'").fetchall()
    total = conn.execute("SELECT COUNT(*) FROM pets").fetchone()[0]
    conn.close()
    assert total == 2, f"the fixture did not split into two groups (got {total})"
    assert len(named) == 1, f"{len(named)} groups came back called Kira"


def test_a_name_still_reaches_the_group_that_inherited_it(tmp_path):
    """The guard on the rule above: refusing to name two groups must not turn
    into refusing to name either."""
    _, conn = _two_groups_from_one_named_pet(tmp_path)

    pc.cluster_pets(conn, Config.load())

    named = conn.execute("SELECT id FROM pets WHERE name='Kira'").fetchone()
    assert named is not None, "the name reached no group at all"
    held = conn.execute(
        "SELECT COUNT(*) FROM animal_detections WHERE pet_id=?", (named["id"],)
    ).fetchone()[0]
    conn.close()
    assert held == 2, "Kira came back holding the wrong detections"


def test_a_single_shared_detection_does_not_carry_a_name(tmp_path):
    """A coincidence is not an inheritance. One detection in common between an
    old pet and a new group of many is the kind of overlap the floor exists to
    turn down -- otherwise a name wanders onto whichever group happens to have
    swept up one stray photograph of it."""
    db_path = tmp_path / "archive.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/photos','2026-01-01')")
    old_pet = factories.add_pet(conn, name="Rex")
    # One detection was Rex's; the other two never were. All three look alike,
    # so they come back as one group holding a single detection of the old pet.
    for file_id, pet_id in ((1, old_pet), (2, None), (3, None)):
        conn.execute(
            """INSERT INTO files(id,root_id,rel_path,size,mtime,media_type,
                                 first_seen,last_seen)
               VALUES(?,1,?,1,0,'image','2026-01-01','2026-01-01')""",
            (file_id, f"{file_id}.jpg"),
        )
        factories.add_animal_detection(conn, file_id, pet_id=pet_id, embedding=_vec(1, 0, 0))
    conn.execute("UPDATE pets SET detection_count=1 WHERE id=?", (old_pet,))
    conn.commit()

    pc.cluster_pets(conn, Config.load())

    names = [r["name"] for r in conn.execute("SELECT name FROM pets")]
    conn.close()
    assert names == [None], f"a one-detection overlap carried the name: {names}"
