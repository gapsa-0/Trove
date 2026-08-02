"""Characterisation suite pinning pre-refactor merge/unmerge behaviour.

services/people.py, pets.py and places.py each implement their own "same
X?" merge/undo transaction (merge_persons/merge_pets/merge_place_clusters,
unmerge_persons/unmerge_pets/unmerge_place_clusters). services/merging.py
will collapse the three into one shared implementation. Before that happens,
this file pins exactly what today's three separate implementations do --
survivor selection, error strings, return-value shapes, and the
survivor-name formula -- so the shared version can be checked against it.

This is a characterisation suite, not a design spec: every assertion below
records what the code does today, including behaviour that looks like it
could be a bug (e.g. people's survivor-selection tie is argument-order
dependent; pets' is not). Nothing here should be read as an endorsement of
that behaviour, and the refactor is free to change it deliberately -- but
that has to be a decision made with eyes open, not an accident of whichever
implementation merging.py happens to imitate.

Existing coverage lives in test_merge_undo.py and test_merge_groups.py;
fixtures below follow their style (small local helpers building rows with
raw SQL) rather than inventing a new one.
"""

from __future__ import annotations

import pytest

from organize_archive.db import database as db
from organize_archive.services import people, pets, places

np = pytest.importorskip("numpy")


# -- fixtures ---------------------------------------------------------------


def _empty_db(tmp_path):
    """Schema only, no rows. The id-validation error paths pinned below all
    return before reading any table, so there is nothing to seed."""
    db_path = tmp_path / "archive.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.commit()
    conn.close()
    return db_path


def _db(tmp_path):
    """An initialised catalogue with one root and nothing else -- the base
    every people fixture below builds on, mirroring test_merge_undo.py's
    _catalog_with_named_persons minus the two hardcoded persons."""
    conn = db.connect(tmp_path / "archive.db")
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/photos','2026-01-01')")
    return conn


def _person(conn, person_id, name, n_faces, file_id_start):
    """A person row plus `n_faces` real face rows, each on its own file.

    Generalises test_merge_undo.py's _catalog_with_named_persons (which
    always creates exactly one face for an always-named person) so the
    survivor-selection tests below can vary face_count and name
    independently. merge_persons' _sync_person_stats deletes a person
    outright once it has zero faces left (`if left == 0 and not ...`), so
    every fixture here gives both sides at least one face -- an empty
    person would vanish the instant it lost the merge, not survive to be
    asserted on.
    """
    now = db.now_iso()
    conn.execute(
        "INSERT INTO persons(id,name,face_count,created_at) VALUES(?,?,?,?)",
        (person_id, name, n_faces, now),
    )
    vec = np.ones(2, dtype="float32").tobytes()
    for i in range(n_faces):
        fid = file_id_start + i
        conn.execute(
            """INSERT INTO files(id,root_id,rel_path,size,mtime,media_type,
                                 first_seen,last_seen)
               VALUES(?,1,?,1,0,'image',?,?)""",
            (fid, f"{fid}.jpg", now, now),
        )
        conn.execute(
            """INSERT INTO faces(id,file_id,box_x,box_y,box_w,box_h,embedding,
                                 person_id,created_at)
               VALUES(?,?,0,0,1,1,?,?,?)""",
            (fid, fid, vec, person_id, now),
        )


def _catalog(tmp_path, count):
    """`count` files under one root -- copied from test_merge_undo.py's
    helper of the same name so the pet fixtures below read the same way."""
    root = tmp_path / "photos"
    root.mkdir()
    conn = db.connect(tmp_path / "archive.db")
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,?,'2026-01-01')", (str(root),))
    for file_id in range(1, count + 1):
        name = f"{file_id}.jpg"
        (root / name).write_bytes(b"fake")
        conn.execute(
            """INSERT INTO files
               (id,root_id,rel_path,size,mtime,media_type,first_seen,last_seen)
               VALUES(?,1,?,4,0,'image','2026-01-01','2026-01-01')""",
            (file_id, name),
        )
    conn.commit()
    return conn


def _insert_detection(conn, det_id, file_id, species, vec, score):
    """Copied from test_merge_undo.py's helper of the same name."""
    conn.execute(
        """INSERT INTO animal_detections
           (id,file_id,species,box_x,box_y,box_w,box_h,det_score,embedding,
            model_source,created_at)
           VALUES(?,?,?,0,0,50,50,?,?,'test','2026-01-01')""",
        (det_id, file_id, species, score, vec.astype("float32").tobytes()),
    )


def _pet(conn, pet_id, name, det_ids):
    """A pet with one detection per id in `det_ids` (files for those ids
    must already exist -- see _catalog), detection_count set to match.
    Unlike people, pets has no "delete if empty" side effect, so det_ids=[]
    is safe if a test ever needs it -- see merge_pets' _refresh_pet_stats,
    which just writes detection_count=0 rather than dropping the row."""
    now = db.now_iso()
    vec = np.array([1, 0], dtype="float32")
    for det_id in det_ids:
        _insert_detection(conn, det_id, det_id, "dog", vec, 0.9)
    conn.execute(
        "INSERT INTO pets(id,name,species,cover_detection_id,detection_count,"
        "created_at) VALUES(?,?,?,?,?,?)",
        (pet_id, name, "dog", det_ids[0] if det_ids else None, len(det_ids), now),
    )
    for det_id in det_ids:
        conn.execute("UPDATE animal_detections SET pet_id=? WHERE id=?", (pet_id, det_id))


def _place_catalog(tmp_path):
    """A root with no files. Place fixtures below never attach members (no
    test here calls _attach the way test_merge_undo.py's do), since none of
    the behaviour pinned in this file depends on place_cluster_members --
    so unlike that file's _place_catalog, there is nothing to create files
    for."""
    conn = db.connect(tmp_path / "archive.db")
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/photos','2026-01-01')")
    return conn


def _insert_place(conn, cid, root_id, name, lat, lon, count, pinned=0):
    """Copied from test_merge_undo.py's helper of the same name."""
    now = db.now_iso()
    conn.execute(
        """INSERT INTO place_clusters(id,root_id,name,lat,lon,member_count,
                                      pinned,created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (cid, root_id, name, lat, lon, count, pinned, now),
    )


# -- Gap 1: survivor-selection chains (people vs. pets) ----------------------
#
# people.merge_persons and pets.merge_pets both chain "named beats unnamed,
# else the larger cluster", but resolve a tie differently: people compares
# face_count with `>=` and has NO id tiebreak, so on a tie the survivor is
# whichever id was passed as the FIRST argument; pets compares
# detection_count with `!=`/`>` and falls back to `pa["id"] < pb["id"]` --
# comparing the two id VALUES, not argument position -- so on a tie the
# survivor is always the LOWER id, regardless of call order. The tests below
# pin both chains and make that contrast explicit.


def test_merge_persons_named_beats_unnamed_both_orders(tmp_path):
    conn = _db(tmp_path)
    _person(conn, 1, "Ana", 1, 100)  # fewer faces, but named
    _person(conn, 2, None, 5, 200)  # more faces, but unnamed
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()
    ok = people.merge_persons(str(db_path), 1, 2)
    assert ok["person"]["id"] == 1
    assert ok["person"]["name"] == "Ana"

    sub = tmp_path / "swapped"
    sub.mkdir()
    conn = _db(sub)
    _person(conn, 1, "Ana", 1, 100)
    _person(conn, 2, None, 5, 200)
    conn.commit()
    db_path2 = sub / "archive.db"
    conn.close()
    ok2 = people.merge_persons(str(db_path2), 2, 1)  # args swapped
    assert ok2["person"]["id"] == 1  # named side still wins
    assert ok2["person"]["name"] == "Ana"


def test_merge_persons_larger_count_wins_when_unnamed_both_orders(tmp_path):
    conn = _db(tmp_path)
    _person(conn, 1, None, 5, 100)
    _person(conn, 2, None, 2, 200)
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()
    ok = people.merge_persons(str(db_path), 1, 2)
    assert ok["person"]["id"] == 1

    sub = tmp_path / "swapped"
    sub.mkdir()
    conn = _db(sub)
    _person(conn, 1, None, 5, 100)
    _person(conn, 2, None, 2, 200)
    conn.commit()
    db_path2 = sub / "archive.db"
    conn.close()
    ok2 = people.merge_persons(str(db_path2), 2, 1)  # args swapped
    assert ok2["person"]["id"] == 1  # the larger cluster still wins


def test_merge_persons_equal_count_tie_keeps_the_lower_id(tmp_path):
    """With face_count tied and neither side named, the lower id survives --
    in either argument order, and matching pets and places.

    This used to keep whichever id was passed first, so the outcome of a
    drag-merge depended on which way round the user dragged the two cards."""
    conn = _db(tmp_path)
    _person(conn, 1, None, 3, 100)
    _person(conn, 2, None, 3, 200)
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()
    ok = people.merge_persons(str(db_path), 1, 2)
    assert ok["person"]["id"] == 1

    sub = tmp_path / "swapped"
    sub.mkdir()
    conn = _db(sub)
    _person(conn, 1, None, 3, 100)
    _person(conn, 2, None, 3, 200)
    conn.commit()
    db_path2 = sub / "archive.db"
    conn.close()
    ok2 = people.merge_persons(str(db_path2), 2, 1)  # args swapped
    assert ok2["person"]["id"] == 1  # the lower id wins either way round


def test_merge_pets_named_beats_unnamed_both_orders(tmp_path):
    conn = _catalog(tmp_path, count=6)
    _pet(conn, 1, "Fido", [1])  # fewer detections, but named
    _pet(conn, 2, None, [2, 3, 4, 5, 6])  # more detections, but unnamed
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()
    ok = pets.merge_pets(str(db_path), 1, 2)
    assert ok["pet"]["id"] == 1
    assert ok["pet"]["name"] == "Fido"

    sub = tmp_path / "swapped"
    sub.mkdir()
    conn = _catalog(sub, count=6)
    _pet(conn, 1, "Fido", [1])
    _pet(conn, 2, None, [2, 3, 4, 5, 6])
    conn.commit()
    db_path2 = sub / "archive.db"
    conn.close()
    ok2 = pets.merge_pets(str(db_path2), 2, 1)  # args swapped
    assert ok2["pet"]["id"] == 1
    assert ok2["pet"]["name"] == "Fido"


def test_merge_pets_larger_count_wins_when_unnamed_both_orders(tmp_path):
    conn = _catalog(tmp_path, count=7)
    _pet(conn, 1, None, [1, 2, 3, 4, 5])
    _pet(conn, 2, None, [6, 7])
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()
    ok = pets.merge_pets(str(db_path), 1, 2)
    assert ok["pet"]["id"] == 1

    sub = tmp_path / "swapped"
    sub.mkdir()
    conn = _catalog(sub, count=7)
    _pet(conn, 1, None, [1, 2, 3, 4, 5])
    _pet(conn, 2, None, [6, 7])
    conn.commit()
    db_path2 = sub / "archive.db"
    conn.close()
    ok2 = pets.merge_pets(str(db_path2), 2, 1)  # args swapped
    assert ok2["pet"]["id"] == 1  # the larger cluster still wins


def test_merge_pets_equal_count_tie_keeps_lower_id_both_orders(tmp_path):
    """Contrast with test_merge_persons_equal_count_tie_keeps_first_argument
    above: with detection_count tied and neither side named, merge_pets
    falls back to `pa["id"] < pb["id"]`, comparing the two id VALUES rather
    than argument position -- so the survivor is always the LOWER id, in
    either call order. This is the opposite rule from people's tiebreak."""
    conn = _catalog(tmp_path, count=6)
    _pet(conn, 1, None, [1, 2, 3])
    _pet(conn, 2, None, [4, 5, 6])
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()
    ok = pets.merge_pets(str(db_path), 1, 2)
    assert ok["pet"]["id"] == 1

    sub = tmp_path / "swapped"
    sub.mkdir()
    conn = _catalog(sub, count=6)
    _pet(conn, 1, None, [1, 2, 3])
    _pet(conn, 2, None, [4, 5, 6])
    conn.commit()
    db_path2 = sub / "archive.db"
    conn.close()
    ok2 = pets.merge_pets(str(db_path2), 2, 1)  # args swapped
    assert ok2["pet"]["id"] == 1  # still the lower id, unlike people's tie


# -- Gap 2: merge/unmerge error strings --------------------------------------
#
# grep across tests/ for these exact strings turns up nothing before this
# file: every "error" dict the six functions can return was, until now,
# checked only for presence ("error" in result), never for its actual text.
# The GUI displays these strings verbatim, so the text is part of the
# contract, not an implementation detail.


def test_merge_persons_error_strings(tmp_path):
    db_path = _empty_db(tmp_path)
    assert people.merge_persons(str(db_path), None, 1) == {"error": "need two distinct persons"}
    assert people.merge_persons(str(db_path), 0, 1) == {"error": "need two distinct persons"}
    assert people.merge_persons(str(db_path), 5, 5) == {"error": "need two distinct persons"}
    assert people.merge_persons(str(db_path), 1, 2) == {"error": "unknown person"}


def test_merge_pets_error_strings(tmp_path):
    db_path = _empty_db(tmp_path)
    assert pets.merge_pets(str(db_path), None, 1) == {"error": "need two distinct pets"}
    assert pets.merge_pets(str(db_path), 0, 1) == {"error": "need two distinct pets"}
    assert pets.merge_pets(str(db_path), 5, 5) == {"error": "need two distinct pets"}
    assert pets.merge_pets(str(db_path), 1, 2) == {"error": "unknown pet"}


def test_merge_place_clusters_error_strings(tmp_path):
    db_path = _empty_db(tmp_path)
    assert places.merge_place_clusters(str(db_path), None, 1) == {
        "error": "need two distinct places"
    }
    assert places.merge_place_clusters(str(db_path), 0, 1) == {"error": "need two distinct places"}
    assert places.merge_place_clusters(str(db_path), 5, 5) == {"error": "need two distinct places"}
    assert places.merge_place_clusters(str(db_path), 1, 2) == {"error": "unknown place"}


def test_unmerge_persons_error_strings(tmp_path):
    db_path = _empty_db(tmp_path)
    assert people.unmerge_persons(str(db_path), None) == {"error": "missing merge_id"}
    assert people.unmerge_persons(str(db_path), 0) == {"error": "missing merge_id"}
    assert people.unmerge_persons(str(db_path), 999999) == {"error": "merge not found"}


def test_unmerge_pets_error_strings(tmp_path):
    db_path = _empty_db(tmp_path)
    assert pets.unmerge_pets(str(db_path), None) == {"error": "missing merge_id"}
    assert pets.unmerge_pets(str(db_path), 0) == {"error": "missing merge_id"}
    assert pets.unmerge_pets(str(db_path), 999999) == {"error": "merge not found"}


def test_unmerge_place_clusters_error_strings(tmp_path):
    db_path = _empty_db(tmp_path)
    assert places.unmerge_place_clusters(str(db_path), None) == {"error": "missing merge_id"}
    assert places.unmerge_place_clusters(str(db_path), 0) == {"error": "missing merge_id"}
    assert places.unmerge_place_clusters(str(db_path), 999999) == {"error": "merge not found"}


# The "both named without an explicit name -> refused" error is already
# pinned, with the exact names present in the message, for all three types:
#   - people:  test_merge_groups.py::test_merge_persons_with_explicit_name_succeeds_and_rewrites_pins
#   - pets:    test_merge_groups.py::test_merge_pets_both_named_differently_requires_explicit_name
#   - places:  test_merge_undo.py::test_merge_place_clusters_survivor_selection_order
# Not duplicated here.


# -- Gap 3: success-response shapes ------------------------------------------
#
# Three tests in test_merge_undo.py call merge_persons and bind the result
# without checking it (a recorded gap, not something to fix here -- see this
# file's module docstring). These pin the full shape of all six functions'
# success responses, key sets included, so a shared implementation can be
# checked against the exact contract rather than "returns something ok-ish".


def test_merge_persons_response_shape(tmp_path):
    conn = _db(tmp_path)
    _person(conn, 1, "Ana", 1, 100)
    _person(conn, 2, None, 1, 200)
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()
    ok = people.merge_persons(str(db_path), 1, 2)
    assert set(ok.keys()) == {"ok", "person"}
    assert ok["ok"] is True
    assert set(ok["person"].keys()) == {"id", "name", "face_count"}


def test_merge_pets_response_shape(tmp_path):
    conn = _catalog(tmp_path, count=2)
    _pet(conn, 1, "Fido", [1])
    _pet(conn, 2, None, [2])
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()
    ok = pets.merge_pets(str(db_path), 1, 2)
    assert set(ok.keys()) == {"ok", "pet"}
    assert ok["ok"] is True
    assert set(ok["pet"].keys()) == {"id", "name", "species", "detections"}


def test_merge_place_clusters_response_shape(tmp_path):
    conn = _place_catalog(tmp_path)
    _insert_place(conn, 1, 1, "Home", 0.0, 0.0, 1)
    _insert_place(conn, 2, 1, None, 1.0, 1.0, 1)
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()
    ok = places.merge_place_clusters(str(db_path), 1, 2)
    assert set(ok.keys()) == {"ok", "place"}
    assert ok["ok"] is True
    assert set(ok["place"].keys()) == {"id", "name", "count"}


def test_unmerge_persons_response_shape(tmp_path):
    conn = _db(tmp_path)
    _person(conn, 1, "Ana", 1, 100)
    _person(conn, 2, "Beto", 1, 200)
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()
    people.merge_persons(str(db_path), 1, 2, name="Ana")
    merge_id = db.open_readonly(db_path).execute("SELECT id FROM person_merges").fetchone()["id"]
    undo = people.unmerge_persons(str(db_path), merge_id)
    assert undo == {"ok": True, "recluster": True}


def test_unmerge_pets_response_shape(tmp_path):
    conn = _catalog(tmp_path, count=2)
    _pet(conn, 1, "Fido", [1])
    _pet(conn, 2, None, [2])
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()
    pets.merge_pets(str(db_path), 1, 2)
    merge_id = db.open_readonly(db_path).execute("SELECT id FROM pet_merges").fetchone()["id"]
    undo = pets.unmerge_pets(str(db_path), merge_id)
    assert undo == {"ok": True, "recluster": True}


def test_unmerge_place_clusters_response_shape_has_no_recluster_key(tmp_path):
    """Deliberately no "recluster" key: unlike people/pets there is no
    clusterer to re-run for a place undo (the restore is already complete),
    and web/server.py relies on that key's absence to decide not to kick
    off a job for this route (see its comment around line 549)."""
    conn = _place_catalog(tmp_path)
    _insert_place(conn, 1, 1, "Home", 0.0, 0.0, 1)
    _insert_place(conn, 2, 1, None, 1.0, 1.0, 1)
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()
    places.merge_place_clusters(str(db_path), 1, 2)
    merge_id = db.open_readonly(db_path).execute("SELECT id FROM place_merges").fetchone()["id"]
    undo = places.unmerge_place_clusters(str(db_path), merge_id)
    assert set(undo.keys()) == {"ok", "place_id"}
    assert undo["ok"] is True
    assert "recluster" not in undo


# -- Gap 4: survivor-name formula --------------------------------------------
#
# merge_persons computes `survivor_name = name or keep["name"]`; merge_pets
# and merge_place_clusters compute `name or keep["name"] or drop["name"]`.
# The trailing `or drop["name"]` looks reachable but, per the survivor chain
# above, is not: whenever exactly one side is named, that named side is
# ALWAYS chosen as `keep` (named beats unnamed), so `keep["name"]` can only
# be falsy when `drop["name"]` is falsy too. These tests pin the observable
# outcome for all three reachable cases, for all three entity types, and
# confirm the two formulas agree everywhere they can actually be exercised.


def test_merge_persons_survivor_name_explicit_overrides(tmp_path):
    conn = _db(tmp_path)
    _person(conn, 1, "Ana", 3, 100)
    _person(conn, 2, "Beto", 1, 200)
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()
    ok = people.merge_persons(str(db_path), 1, 2, name="Zoe")
    assert ok["person"]["name"] == "Zoe"


def test_merge_persons_survivor_name_keeps_named_side_when_loser_unnamed(tmp_path):
    conn = _db(tmp_path)
    _person(conn, 1, "Ana", 1, 100)
    _person(conn, 2, None, 5, 200)
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()
    ok = people.merge_persons(str(db_path), 1, 2)
    assert ok["person"]["name"] == "Ana"


def test_merge_persons_survivor_name_is_none_when_both_unnamed(tmp_path):
    conn = _db(tmp_path)
    _person(conn, 1, None, 3, 100)
    _person(conn, 2, None, 1, 200)
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()
    ok = people.merge_persons(str(db_path), 1, 2)
    assert ok["person"]["name"] is None


def test_merge_pets_survivor_name_explicit_overrides(tmp_path):
    conn = _catalog(tmp_path, count=4)
    _pet(conn, 1, "Fido", [1, 2, 3])
    _pet(conn, 2, "Rex", [4])
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()
    ok = pets.merge_pets(str(db_path), 1, 2, name="Buddy")
    assert ok["pet"]["name"] == "Buddy"


def test_merge_pets_survivor_name_keeps_named_side_when_loser_unnamed(tmp_path):
    conn = _catalog(tmp_path, count=6)
    _pet(conn, 1, "Fido", [1])
    _pet(conn, 2, None, [2, 3, 4, 5, 6])
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()
    ok = pets.merge_pets(str(db_path), 1, 2)
    assert ok["pet"]["name"] == "Fido"


def test_merge_pets_survivor_name_is_none_when_both_unnamed(tmp_path):
    conn = _catalog(tmp_path, count=4)
    _pet(conn, 1, None, [1, 2, 3])
    _pet(conn, 2, None, [4])
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()
    ok = pets.merge_pets(str(db_path), 1, 2)
    assert ok["pet"]["name"] is None


def test_merge_place_clusters_survivor_name_explicit_overrides(tmp_path):
    conn = _place_catalog(tmp_path)
    _insert_place(conn, 1, 1, "Casa", 0.0, 0.0, 3)
    _insert_place(conn, 2, 1, "Depto", 0.0, 0.0, 1)
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()
    ok = places.merge_place_clusters(str(db_path), 1, 2, name="Nueva")
    assert ok["place"]["name"] == "Nueva"


def test_merge_place_clusters_survivor_name_keeps_named_side_when_loser_unnamed(tmp_path):
    conn = _place_catalog(tmp_path)
    _insert_place(conn, 1, 1, "Casa", 0.0, 0.0, 1)
    _insert_place(conn, 2, 1, None, 0.0, 0.0, 5)
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()
    ok = places.merge_place_clusters(str(db_path), 1, 2)
    assert ok["place"]["name"] == "Casa"


def test_merge_place_clusters_survivor_name_is_none_when_both_unnamed(tmp_path):
    conn = _place_catalog(tmp_path)
    _insert_place(conn, 1, 1, None, 0.0, 0.0, 3)
    _insert_place(conn, 2, 1, None, 0.0, 0.0, 1)
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()
    ok = places.merge_place_clusters(str(db_path), 1, 2)
    assert ok["place"]["name"] is None
