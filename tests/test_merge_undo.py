"""Undoing a drag-merge (Part C) and detaching a file from a person (Part D).

Merging is already covered by test_merge_groups.py; this file covers what
happens *after*: person_merges/pet_merges bookkeeping, unmerge_persons/
unmerge_pets reversing it (including the cannot-link that stops a silent
re-merge), pets/cluster.py's _apply_links now honouring 'different', and
detach_file_from_person's durable per-face cannot-link + person_files cleanup.
"""

from __future__ import annotations

import json

import pytest

from organize_archive.config import Config
from organize_archive.db import database as db
from organize_archive.gui import queries
from organize_archive.pets import cluster as pets_cluster

np = pytest.importorskip("numpy")


def _catalog(tmp_path, count):
    root = tmp_path / "photos"
    root.mkdir()
    conn = db.connect(tmp_path / "archive.db")
    db.init_db(conn)
    conn.execute(
        "INSERT INTO roots(id,path,added_at) VALUES(1,?,'2026-01-01')", (str(root),))
    for file_id in range(1, count + 1):
        name = f"{file_id}.jpg"
        (root / name).write_bytes(b"fake")
        conn.execute(
            """INSERT INTO files
               (id,root_id,rel_path,size,mtime,media_type,first_seen,last_seen)
               VALUES(?,1,?,4,0,'image','2026-01-01','2026-01-01')""",
            (file_id, name))
    conn.commit()
    return conn


def _insert_detection(conn, det_id, file_id, species, vector, score):
    conn.execute(
        """INSERT INTO animal_detections
           (id,file_id,species,box_x,box_y,box_w,box_h,det_score,embedding,
            model_source,created_at)
           VALUES(?,?,?,0,0,50,50,?,?,'test','2026-01-01')""",
        (det_id, file_id, species, score, vector.astype("float32").tobytes()))


def _catalog_with_named_persons(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/photos','2026-01-01')")
    for file_id in (1, 2):
        conn.execute(
            """INSERT INTO files(id,root_id,rel_path,size,mtime,media_type,
                                 first_seen,last_seen)
               VALUES(?,1,?,1,0,'image','2026-01-01','2026-01-01')""",
            (file_id, f"{file_id}.jpg"))
    now = db.now_iso()
    conn.execute(
        "INSERT INTO persons(id,name,face_count,created_at) VALUES(1,'Ana',1,?)", (now,))
    conn.execute(
        "INSERT INTO persons(id,name,face_count,created_at) VALUES(2,'Beto',1,?)", (now,))
    vec = np.ones(2, dtype="float32").tobytes()
    conn.execute(
        """INSERT INTO faces(id,file_id,box_x,box_y,box_w,box_h,embedding,
                             person_id,manual_person,created_at)
           VALUES(1,1,0,0,1,1,?,1,'Ana',?)""", (vec, now))
    conn.execute(
        """INSERT INTO faces(id,file_id,box_x,box_y,box_w,box_h,embedding,
                             person_id,manual_person,created_at)
           VALUES(2,2,0,0,1,1,?,2,'Beto',?)""", (vec, now))
    conn.commit()
    conn.close()
    return db_path


# -- recording -----------------------------------------------------------

def test_merge_persons_records_a_person_merges_row(tmp_path):
    db_path = _catalog_with_named_persons(tmp_path)
    ok = queries.merge_persons(str(db_path), 1, 2, name="Ana")
    assert ok["ok"] is True

    check = db.open_readonly(db_path)
    row = check.execute("SELECT * FROM person_merges").fetchone()
    assert row is not None
    assert row["survivor_name"] == "Ana"
    assert row["dropped_name"] == "Beto"
    assert json.loads(row["face_ids"]) == [2]   # Beto's face(s), moved by the merge
    assert row["link_a"] is not None and row["link_b"] is not None
    link = check.execute(
        "SELECT kind FROM face_links WHERE face_a=? AND face_b=?",
        (row["link_a"], row["link_b"])).fetchone()
    assert link["kind"] == "same"
    check.close()


def test_merge_pets_records_a_pet_merges_row(tmp_path):
    conn = _catalog(tmp_path, count=2)
    _insert_detection(conn, 1, 1, "dog", np.array([1, 0], dtype="float32"), .9)
    _insert_detection(conn, 2, 2, "dog", np.array([1, 0], dtype="float32"), .9)
    now = db.now_iso()
    conn.execute(
        "INSERT INTO pets(id,name,species,cover_detection_id,detection_count,"
        "created_at) VALUES(1,'Fido','dog',1,1,?)", (now,))
    conn.execute(
        "INSERT INTO pets(id,name,species,cover_detection_id,detection_count,"
        "created_at) VALUES(2,NULL,'dog',2,1,?)", (now,))
    conn.execute("UPDATE animal_detections SET pet_id=1 WHERE id=1")
    conn.execute("UPDATE animal_detections SET pet_id=2 WHERE id=2")
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()

    ok = queries.merge_pets(str(db_path), 1, 2)
    assert ok["ok"] is True

    check = db.open_readonly(db_path)
    row = check.execute("SELECT * FROM pet_merges").fetchone()
    assert row is not None
    assert row["survivor_name"] == "Fido"
    assert json.loads(row["det_ids"]) == [2]
    link = check.execute(
        "SELECT kind FROM pet_links WHERE det_a=? AND det_b=?",
        (row["link_a"], row["link_b"])).fetchone()
    assert link["kind"] == "same"
    check.close()


# -- unmerge_persons ------------------------------------------------------

def test_unmerge_persons_reverses_the_link_and_restores_names(tmp_path):
    db_path = _catalog_with_named_persons(tmp_path)
    ok = queries.merge_persons(str(db_path), 1, 2, name="Ana")
    survivor_id = ok["person"]["id"]

    merge_row = db.open_readonly(db_path).execute(
        "SELECT id FROM person_merges").fetchone()
    merge_id = merge_row["id"]

    undo = queries.unmerge_persons(str(db_path), merge_id)
    assert undo["ok"] is True
    assert undo["recluster"] is True

    check = db.open_readonly(db_path)
    assert check.execute("SELECT COUNT(*) FROM person_merges").fetchone()[0] == 0
    # the 'same' link is gone, replaced with a 'different' cannot-link
    links = check.execute("SELECT face_a, face_b, kind FROM face_links").fetchall()
    assert len(links) == 1
    assert links[0]["kind"] == "different"
    # face 2 (Beto's) is pinned back to "Beto", not left pointing at "Ana"
    pins = {r["id"]: r["manual_person"] for r in
            check.execute("SELECT id, manual_person FROM faces")}
    assert pins[1] == "Ana"
    assert pins[2] == "Beto"
    check.close()


def test_unmerge_persons_twice_errors_cleanly(tmp_path):
    db_path = _catalog_with_named_persons(tmp_path)
    ok = queries.merge_persons(str(db_path), 1, 2, name="Ana")
    merge_id = db.open_readonly(db_path).execute(
        "SELECT id FROM person_merges").fetchone()["id"]

    first = queries.unmerge_persons(str(db_path), merge_id)
    assert first["ok"] is True
    second = queries.unmerge_persons(str(db_path), merge_id)
    assert "error" in second

    # a bogus id (never existed) is likewise a clean error, not a crash
    assert "error" in queries.unmerge_persons(str(db_path), 999999)


def test_recluster_after_undo_does_not_remerge(tmp_path):
    """Exercises faces/cluster.py's _apply_links directly (cheaper than
    running the full clusterer): once unmerge_persons has replaced the 'same'
    link with 'different', a must-link chain elsewhere must not be able to
    override that cannot-link and reunite the two clusters."""
    from organize_archive.faces.cluster import _apply_links

    db_path = _catalog_with_named_persons(tmp_path)
    ok = queries.merge_persons(str(db_path), 1, 2, name="Ana")
    merge_id = db.open_readonly(db_path).execute(
        "SELECT id FROM person_merges").fetchone()["id"]
    queries.unmerge_persons(str(db_path), merge_id)

    conn = db.connect(db_path)
    # Simulate the clusterer having (re)split the two faces into separate
    # automatic clusters again, each a singleton.
    face_ids = [1, 2]
    cluster_list = [[0], [1]]   # positions into face_ids
    result = _apply_links(conn, cluster_list, face_ids)
    # The cannot-link must block any union: still two separate clusters.
    assert len(result) == 2
    conn.close()


# -- unmerge_pets ---------------------------------------------------------

def test_unmerge_pets_reverses_the_link(tmp_path):
    conn = _catalog(tmp_path, count=2)
    _insert_detection(conn, 1, 1, "dog", np.array([1, 0], dtype="float32"), .9)
    _insert_detection(conn, 2, 2, "dog", np.array([1, 0], dtype="float32"), .9)
    now = db.now_iso()
    conn.execute(
        "INSERT INTO pets(id,name,species,cover_detection_id,detection_count,"
        "created_at) VALUES(1,'Fido','dog',1,1,?)", (now,))
    conn.execute(
        "INSERT INTO pets(id,name,species,cover_detection_id,detection_count,"
        "created_at) VALUES(2,NULL,'dog',2,1,?)", (now,))
    conn.execute("UPDATE animal_detections SET pet_id=1 WHERE id=1")
    conn.execute("UPDATE animal_detections SET pet_id=2 WHERE id=2")
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()

    queries.merge_pets(str(db_path), 1, 2)
    merge_id = db.open_readonly(db_path).execute(
        "SELECT id FROM pet_merges").fetchone()["id"]

    undo = queries.unmerge_pets(str(db_path), merge_id)
    assert undo["ok"] is True
    assert undo["recluster"] is True

    check = db.open_readonly(db_path)
    assert check.execute("SELECT COUNT(*) FROM pet_merges").fetchone()[0] == 0
    links = check.execute("SELECT kind FROM pet_links").fetchall()
    assert len(links) == 1 and links[0]["kind"] == "different"
    check.close()

    # calling it again is a clean error
    assert "error" in queries.unmerge_pets(str(db_path), merge_id)


def test_pets_apply_links_honours_a_different_cannot_link(tmp_path):
    """A must-link chain (A-B same, B-C same) would normally union A/B/C into
    one group; an explicit 'different' between A and C must still keep them
    apart, mirroring faces/cluster.py's cannot-link-wins rule."""
    conn = _catalog(tmp_path, count=3)
    _insert_detection(conn, 1, 1, "dog", np.array([1, 0], dtype="float32"), .9)
    _insert_detection(conn, 2, 2, "dog", np.array([1, 0], dtype="float32"), .9)
    _insert_detection(conn, 3, 3, "dog", np.array([1, 0], dtype="float32"), .9)
    now = db.now_iso()
    conn.execute(
        "INSERT INTO pet_links(det_a,det_b,kind,created_at) VALUES(1,2,'same',?)", (now,))
    conn.execute(
        "INSERT INTO pet_links(det_a,det_b,kind,created_at) VALUES(2,3,'same',?)", (now,))
    conn.execute(
        "INSERT INTO pet_links(det_a,det_b,kind,created_at) VALUES(1,3,'different',?)", (now,))
    conn.commit()

    emb_rows = conn.execute(
        "SELECT id FROM animal_detections ORDER BY id").fetchall()
    groups = [[0], [1], [2]]   # three singleton groups: det 1, 2, 3
    result = pets_cluster._apply_links(conn, groups, emb_rows)
    # 1 and 3 must never end up in the same group, even though both are
    # must-linked to 2.
    det_to_group = {}
    for gi, idxs in enumerate(result):
        for i in idxs:
            det_to_group[emb_rows[i]["id"]] = gi
    assert det_to_group[1] != det_to_group[3]
    conn.close()


# -- detach_file_from_person ----------------------------------------------

def _catalog_for_detach(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/photos','2026-01-01')")
    for file_id in (1, 2):
        conn.execute(
            """INSERT INTO files(id,root_id,rel_path,size,mtime,media_type,
                                 first_seen,last_seen)
               VALUES(?,1,?,1,0,'image','2026-01-01','2026-01-01')""",
            (file_id, f"{file_id}.jpg"))
    now = db.now_iso()
    conn.execute(
        "INSERT INTO persons(id,name,face_count,cover_face_id,created_at) "
        "VALUES(1,'Ana',2,1,?)", (now,))
    vec = np.ones(2, dtype="float32").tobytes()
    conn.execute(
        """INSERT INTO faces(id,file_id,box_x,box_y,box_w,box_h,embedding,
                             person_id,manual_person,created_at)
           VALUES(1,1,0,0,1,1,?,1,'Ana',?)""", (vec, now))
    conn.execute(
        """INSERT INTO faces(id,file_id,box_x,box_y,box_w,box_h,embedding,
                             person_id,manual_person,created_at)
           VALUES(2,2,0,0,1,1,?,1,'Ana',?)""", (vec, now))
    conn.execute(
        "INSERT INTO person_files(person_id,file_id,person_name,created_at) "
        "VALUES(1,2,'Ana',?)", (now,))
    conn.commit()
    conn.close()
    return db_path


def test_detach_file_from_person_clears_face_and_writes_cannot_link(tmp_path):
    db_path = _catalog_for_detach(tmp_path)
    res = queries.detach_file_from_person(str(db_path), 1, 2)
    assert res["ok"] is True
    assert res["detached_faces"] == 1

    check = db.open_readonly(db_path)
    face = check.execute("SELECT person_id, manual_person FROM faces WHERE id=2").fetchone()
    assert face["person_id"] is None
    assert face["manual_person"] is None
    # the untouched face (file 1, still Ana) is unaffected
    other = check.execute("SELECT person_id FROM faces WHERE id=1").fetchone()
    assert other["person_id"] == 1
    # a durable cannot-link now exists between the detached face and Ana's rep face
    link = check.execute(
        "SELECT kind FROM face_links WHERE face_a=1 AND face_b=2").fetchone()
    assert link is not None and link["kind"] == "different"
    # the manual person_files tag for (person 1, file 2) is gone
    assert check.execute(
        "SELECT 1 FROM person_files WHERE person_id=1 AND file_id=2").fetchone() is None
    # the person's stats reflect only the remaining face
    assert check.execute("SELECT face_count FROM persons WHERE id=1").fetchone()[0] == 1
    check.close()


def test_detach_file_from_person_unknown_inputs_error_cleanly(tmp_path):
    db_path = _catalog_for_detach(tmp_path)
    assert "error" in queries.detach_file_from_person(str(db_path), None, 2)
    assert "error" in queries.detach_file_from_person(str(db_path), 1, None)
    assert "error" in queries.detach_file_from_person(str(db_path), 999, 2)
    # file 1 has a face of person 1, but this asks about file 2's OTHER
    # (nonexistent) association -- wrong file/person pair with no matching face
    assert "error" in queries.detach_file_from_person(str(db_path), 1, 999999)


# -- merge_place_clusters / unmerge_place_clusters ------------------------
#
# Places are durable (place_clusters rows are never rebuilt wholesale -- see
# place_merges' schema comment), so unlike the persons/pets tests above,
# there's no face_links/pet_links-style constraint and no recluster step:
# a merge moves rows once, and undo restores them exactly.

def _place_catalog(tmp_path, *, root_ids=(1,)):
    """Base catalog: one root per id in `root_ids`, and 4 files (ids 1-4) all
    under the first root -- enough members to split across two places without
    needing a real GPS scan. Callers add their own place_clusters/
    place_cluster_members rows on top, mirroring how the pet tests above
    insert pets/animal_detections by hand."""
    conn = db.connect(tmp_path / "archive.db")
    db.init_db(conn)
    now = db.now_iso()
    for rid in root_ids:
        conn.execute("INSERT INTO roots(id,path,added_at) VALUES(?,?,?)",
                     (rid, f"/root{rid}", now))
    for file_id in (1, 2, 3, 4):
        conn.execute(
            """INSERT INTO files(id,root_id,rel_path,size,mtime,media_type,
                                 first_seen,last_seen)
               VALUES(?,?,?,1,0,'image',?,?)""",
            (file_id, root_ids[0], f"{file_id}.jpg", now, now))
    conn.commit()
    return conn


def _insert_place(conn, cid, root_id, name, lat, lon, count, pinned=0):
    now = db.now_iso()
    conn.execute(
        """INSERT INTO place_clusters(id,root_id,name,lat,lon,member_count,
                                      pinned,created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (cid, root_id, name, lat, lon, count, pinned, now))


def _attach(conn, cid, file_id, source="auto"):
    conn.execute(
        "INSERT INTO place_cluster_members(cluster_id,file_id,source) VALUES(?,?,?)",
        (cid, file_id, source))


def test_merge_place_clusters_records_a_place_merges_row(tmp_path):
    conn = _place_catalog(tmp_path)
    _insert_place(conn, 1, 1, "Home", 10.0, 20.0, 2)
    _insert_place(conn, 2, 1, None, 12.0, 22.0, 2)
    _attach(conn, 1, 1, "auto")
    _attach(conn, 1, 2, "manual")
    _attach(conn, 2, 3, "auto")
    _attach(conn, 2, 4, "auto")
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()

    ok = queries.merge_place_clusters(str(db_path), 1, 2)
    assert ok["ok"] is True
    assert ok["place"]["id"] == 1
    assert ok["place"]["name"] == "Home"
    assert ok["place"]["count"] == 4

    check = db.open_readonly(db_path)
    row = check.execute("SELECT * FROM place_merges").fetchone()
    assert row is not None
    assert row["survivor_id"] == 1
    assert row["dropped_name"] is None
    assert row["survivor_lat"] == 10.0 and row["survivor_lon"] == 20.0
    assert row["dropped_lat"] == 12.0 and row["dropped_lon"] == 22.0
    moved = json.loads(row["file_ids"])
    assert sorted(moved) == [[3, "auto"], [4, "auto"]]
    # both files 3 and 4 now belong to the survivor
    members = {r["file_id"] for r in
               check.execute("SELECT file_id FROM place_cluster_members WHERE cluster_id=1")}
    assert members == {1, 2, 3, 4}
    check.close()


def test_merge_place_clusters_survivor_selection_order(tmp_path):
    # named beats unnamed
    conn = _place_catalog(tmp_path)
    _insert_place(conn, 1, 1, None, 0.0, 0.0, 5)
    _insert_place(conn, 2, 1, "Park", 0.0, 0.0, 1)
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()
    ok = queries.merge_place_clusters(str(db_path), 1, 2)
    assert ok["place"]["id"] == 2 and ok["place"]["name"] == "Park"

    # pinned beats unpinned, both unnamed
    sub = tmp_path / "pinned"; sub.mkdir()
    conn = _place_catalog(sub)
    _insert_place(conn, 1, 1, None, 0.0, 0.0, 1, pinned=0)
    _insert_place(conn, 2, 1, None, 5.0, 5.0, 9, pinned=1)
    conn.commit()
    db_path2 = sub / "archive.db"
    conn.close()
    ok = queries.merge_place_clusters(str(db_path2), 1, 2)
    assert ok["place"]["id"] == 2  # pinned wins despite the smaller count

    # neither named nor pinned: larger member_count wins
    sub = tmp_path / "count"; sub.mkdir()
    conn = _place_catalog(sub)
    _insert_place(conn, 1, 1, None, 0.0, 0.0, 2)
    _insert_place(conn, 2, 1, None, 0.0, 0.0, 7)
    conn.commit()
    db_path3 = sub / "archive.db"
    conn.close()
    ok = queries.merge_place_clusters(str(db_path3), 1, 2)
    assert ok["place"]["id"] == 2

    # tied on everything: lower id wins
    sub = tmp_path / "tie"; sub.mkdir()
    conn = _place_catalog(sub)
    _insert_place(conn, 1, 1, None, 0.0, 0.0, 3)
    _insert_place(conn, 2, 1, None, 0.0, 0.0, 3)
    conn.commit()
    db_path4 = sub / "archive.db"
    conn.close()
    ok = queries.merge_place_clusters(str(db_path4), 1, 2)
    assert ok["place"]["id"] == 1

    # two different names, no explicit name -> refused
    sub = tmp_path / "clash"; sub.mkdir()
    conn = _place_catalog(sub)
    _insert_place(conn, 1, 1, "Casa", 0.0, 0.0, 1)
    _insert_place(conn, 2, 1, "Depto", 0.0, 0.0, 1)
    conn.commit()
    db_path5 = sub / "archive.db"
    conn.close()
    err = queries.merge_place_clusters(str(db_path5), 1, 2)
    assert "error" in err and "Casa" in err["error"] and "Depto" in err["error"]


def test_merge_place_clusters_weighted_centroid(tmp_path):
    conn = _place_catalog(tmp_path)
    # place 1 (survivor, named, count=1) at (0,0); place 2 (count=3) at (10,10)
    # weighted mean: (0*1 + 10*3)/4 = 7.5 for both lat/lon
    _insert_place(conn, 1, 1, "Home", 0.0, 0.0, 1)
    _insert_place(conn, 2, 1, None, 10.0, 10.0, 3)
    _attach(conn, 1, 1)
    _attach(conn, 2, 2); _attach(conn, 2, 3); _attach(conn, 2, 4)
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()

    queries.merge_place_clusters(str(db_path), 1, 2)
    check = db.open_readonly(db_path)
    row = check.execute("SELECT lat, lon FROM place_clusters WHERE id=1").fetchone()
    assert row["lat"] == pytest.approx(7.5)
    assert row["lon"] == pytest.approx(7.5)
    check.close()


def test_merge_place_clusters_pinned_survivor_coordinate_does_not_move(tmp_path):
    conn = _place_catalog(tmp_path)
    _insert_place(conn, 1, 1, "Home", 1.0, 2.0, 1, pinned=1)
    _insert_place(conn, 2, 1, None, 50.0, 60.0, 9)
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()

    queries.merge_place_clusters(str(db_path), 1, 2)
    check = db.open_readonly(db_path)
    row = check.execute("SELECT lat, lon FROM place_clusters WHERE id=1").fetchone()
    assert row["lat"] == 1.0 and row["lon"] == 2.0
    check.close()


def test_merge_place_clusters_refuses_cross_root(tmp_path):
    conn = _place_catalog(tmp_path, root_ids=(1, 2))
    _insert_place(conn, 1, 1, "Home", 0.0, 0.0, 1)
    _insert_place(conn, 2, 2, "Office", 5.0, 5.0, 1)
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()

    err = queries.merge_place_clusters(str(db_path), 1, 2)
    assert "error" in err
    # nothing moved: both places still exist
    check = db.open_readonly(db_path)
    assert check.execute("SELECT COUNT(*) FROM place_clusters").fetchone()[0] == 2
    check.close()


def test_unmerge_place_clusters_restores_the_dropped_place(tmp_path):
    conn = _place_catalog(tmp_path)
    # place 1: named, unpinned -> survivor (named beats pinned-but-unnamed).
    # place 2: unnamed but pinned -> dropped; its name/lat/lon/pinned must
    # all come back exactly on undo.
    _insert_place(conn, 1, 1, "Home", 0.0, 0.0, 2)
    _insert_place(conn, 2, 1, None, 10.0, 10.0, 2, pinned=1)
    _attach(conn, 1, 1, "auto")
    _attach(conn, 1, 2, "manual")
    _attach(conn, 2, 3, "auto")
    _attach(conn, 2, 4, "manual")
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()

    ok = queries.merge_place_clusters(str(db_path), 1, 2)
    assert ok["ok"] is True
    assert ok["place"]["id"] == 1   # the named side survives
    merge_id = db.open_readonly(db_path).execute(
        "SELECT id FROM place_merges").fetchone()["id"]

    undo = queries.unmerge_place_clusters(str(db_path), merge_id)
    assert undo["ok"] is True
    assert "recluster" not in undo   # nothing queued: places need no rebuild
    restored_id = undo["place_id"]

    check = db.open_readonly(db_path)
    assert check.execute("SELECT COUNT(*) FROM place_merges").fetchone()[0] == 0
    restored = check.execute(
        "SELECT name, lat, lon, pinned, member_count FROM place_clusters WHERE id=?",
        (restored_id,)).fetchone()
    assert restored["name"] is None
    assert restored["lat"] == 10.0 and restored["lon"] == 10.0
    assert restored["pinned"] == 1
    assert restored["member_count"] == 2
    restored_members = {(r["file_id"], r["source"]) for r in check.execute(
        "SELECT file_id, source FROM place_cluster_members WHERE cluster_id=?",
        (restored_id,))}
    assert restored_members == {(3, "auto"), (4, "manual")}
    # the survivor's centroid and count are back to their pre-merge values
    survivor = check.execute(
        "SELECT lat, lon, member_count FROM place_clusters WHERE id=1").fetchone()
    assert survivor["lat"] == 0.0 and survivor["lon"] == 0.0
    assert survivor["member_count"] == 2
    check.close()


def test_unmerge_place_clusters_restores_the_survivors_own_name(tmp_path):
    """Merging two differently-named places takes an explicit `name` that
    overwrites the survivor's own name. Undo has to put that back, or the
    pre-merge pair "Casa"/"Depto" comes back as "Casa"/"Casa" and a name the
    user typed is gone for good."""
    conn = _place_catalog(tmp_path)
    _insert_place(conn, 1, 1, "Casa", 0.0, 0.0, 2)    # survivor (larger count)
    _insert_place(conn, 2, 1, "Depto", 1.0, 1.0, 1)
    _attach(conn, 1, 1); _attach(conn, 1, 2)
    _attach(conn, 2, 3)
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()

    ok = queries.merge_place_clusters(str(db_path), 1, 2, name="Depto")
    assert ok["place"]["id"] == 1 and ok["place"]["name"] == "Depto"
    merge_id = db.open_readonly(db_path).execute(
        "SELECT id FROM place_merges").fetchone()["id"]

    undo = queries.unmerge_place_clusters(str(db_path), merge_id)
    restored_id = undo["place_id"]
    check = db.open_readonly(db_path)
    names = {r["id"]: r["name"] for r in check.execute(
        "SELECT id, name FROM place_clusters")}
    assert names[1] == "Casa"              # survivor's own name is back
    assert names[restored_id] == "Depto"   # the dropped place kept its own
    check.close()


def test_unmerge_place_clusters_keeps_a_rename_made_after_the_merge(tmp_path):
    """The name restore above is deliberately conditional: renaming the
    survivor after merging is a later, explicit user decision, and undoing
    the merge must not silently roll it back."""
    conn = _place_catalog(tmp_path)
    _insert_place(conn, 1, 1, "Casa", 0.0, 0.0, 2)
    _insert_place(conn, 2, 1, "Depto", 1.0, 1.0, 1)
    _attach(conn, 1, 1); _attach(conn, 1, 2)
    _attach(conn, 2, 3)
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()

    queries.merge_place_clusters(str(db_path), 1, 2, name="Depto")
    merge_id = db.open_readonly(db_path).execute(
        "SELECT id FROM place_merges").fetchone()["id"]
    queries.rename_place_cluster(str(db_path), 1, "Casa de la abuela")

    queries.unmerge_place_clusters(str(db_path), merge_id)
    check = db.open_readonly(db_path)
    assert check.execute(
        "SELECT name FROM place_clusters WHERE id=1").fetchone()["name"] == "Casa de la abuela"
    check.close()


def test_unmerge_place_clusters_twice_errors_cleanly(tmp_path):
    conn = _place_catalog(tmp_path)
    _insert_place(conn, 1, 1, "Home", 0.0, 0.0, 1)
    _insert_place(conn, 2, 1, None, 1.0, 1.0, 1)
    _attach(conn, 1, 1)
    _attach(conn, 2, 2)
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()

    queries.merge_place_clusters(str(db_path), 1, 2)
    merge_id = db.open_readonly(db_path).execute(
        "SELECT id FROM place_merges").fetchone()["id"]

    first = queries.unmerge_place_clusters(str(db_path), merge_id)
    assert first["ok"] is True
    second = queries.unmerge_place_clusters(str(db_path), merge_id)
    assert "error" in second
    assert "error" in queries.unmerge_place_clusters(str(db_path), 999999)


def test_place_merge_survives_assign_unplaced(tmp_path):
    """The durability claim this whole design rests on: a subsequent
    assign_unplaced run (the pipeline's normal incremental pass) must not
    disturb an already-merged place -- it only ever adds new unplaced
    points, never touches existing members."""
    from organize_archive.geo.clusters import assign_unplaced

    conn = _place_catalog(tmp_path)
    _insert_place(conn, 1, 1, "Home", 0.0, 0.0, 2)
    _insert_place(conn, 2, 1, None, 0.001, 0.001, 2)  # a few meters away
    _attach(conn, 1, 1); _attach(conn, 1, 2)
    _attach(conn, 2, 3); _attach(conn, 2, 4)
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()

    ok = queries.merge_place_clusters(str(db_path), 1, 2)
    assert ok["ok"] is True
    merged_count = ok["place"]["count"]

    conn = db.connect(db_path)
    stats = assign_unplaced(conn, root_id=1)
    conn.commit()
    assert stats.points == 0   # no unplaced geo files exist in this fixture

    check = db.open_readonly(db_path)
    survivor = check.execute(
        "SELECT member_count FROM place_clusters WHERE id=1").fetchone()
    assert survivor["member_count"] == merged_count
    assert check.execute("SELECT COUNT(*) FROM place_clusters WHERE id=2").fetchone()[0] == 0
    check.close()


# -- place_merge_preview -----------------------------------------------------
#
# The GUI preflight that answers "how spread out would this merge be?" before
# the user confirms a drag-merge. Must track merge_place_clusters' survivor
# and centre logic exactly (both go through _place_merge_survivor), or the
# number shown in the confirmation dialog could disagree with where the merge
# actually lands the centroid.

_WARN_KM = Config().place_merge_warn_km  # exercise the real configured default


def _geo(conn, file_id, lat, lon):
    """A file's coordinate, as geo/clusters.py or the takeout resolver would
    have written it. Deliberately separate from _place_catalog's file rows:
    a place_cluster_members row can exist without one (manual attachment),
    which is exactly the case one of the tests below checks."""
    conn.execute("INSERT INTO geo(file_id, lat, lon) VALUES(?,?,?)", (file_id, lat, lon))


def test_place_merge_preview_tight_cluster_does_not_warn(tmp_path):
    conn = _place_catalog(tmp_path)
    _insert_place(conn, 1, 1, "Home", 0.0, 0.0, 2)
    _insert_place(conn, 2, 1, None, 0.0, 0.0, 2)
    _attach(conn, 1, 1); _attach(conn, 1, 2)
    _attach(conn, 2, 3); _attach(conn, 2, 4)
    # All four members sit essentially on top of each other and of the
    # (0,0)/(0,0) centroids above, so the merged centre has ~nothing to be
    # far from.
    _geo(conn, 1, 0.0, 0.0); _geo(conn, 2, 0.0, 0.00001)
    _geo(conn, 3, 0.00001, 0.0); _geo(conn, 4, 0.0, 0.0)
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()

    res = queries.place_merge_preview(str(db_path), 1, 2, _WARN_KM)
    assert res["ok"] is True
    assert res["span_km"] == pytest.approx(0.0, abs=0.01)
    assert res["warn"] is False
    assert res["threshold_km"] == _WARN_KM


def test_place_merge_preview_far_member_produces_expected_span_and_warns(tmp_path):
    conn = _place_catalog(tmp_path)
    _insert_place(conn, 1, 1, "Home", 0.0, 0.0, 2)
    _insert_place(conn, 2, 1, None, 0.0, 0.0, 2)
    _attach(conn, 1, 1); _attach(conn, 1, 2)
    _attach(conn, 2, 3); _attach(conn, 2, 4)
    # Three members right at the centre, one a full degree of latitude away
    # (~111.2 km -- the standard "1 deg lat" approximation), so the expected
    # span is known up front rather than just asserted to be "big".
    _geo(conn, 1, 0.0, 0.0); _geo(conn, 2, 0.0, 0.0)
    _geo(conn, 3, 0.0, 0.0); _geo(conn, 4, 1.0, 0.0)
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()

    res = queries.place_merge_preview(str(db_path), 1, 2, _WARN_KM)
    assert res["ok"] is True
    assert res["span_km"] == pytest.approx(111.2, abs=0.5)
    assert res["warn"] is True


def test_place_merge_preview_pinned_survivor_centre_is_the_pin(tmp_path):
    conn = _place_catalog(tmp_path)
    # Pinned survivor at (0,0); the other place's centroid is far away, but
    # that centroid is never used as the centre once a pin is involved --
    # the pin coordinate is (see merge_place_clusters' own centroid rule).
    _insert_place(conn, 1, 1, "Home", 0.0, 0.0, 1, pinned=1)
    _insert_place(conn, 2, 1, None, 50.0, 60.0, 2)
    _attach(conn, 1, 1)
    _attach(conn, 2, 2); _attach(conn, 2, 3)
    # Member 2 sits a known distance from the PIN (0,0), not from place 2's
    # own centroid (50,60) -- half a degree of latitude, ~55.6 km.
    _geo(conn, 2, 0.5, 0.0)
    _geo(conn, 3, 0.0, 0.0)
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()

    res = queries.place_merge_preview(str(db_path), 1, 2, _WARN_KM)
    assert res["ok"] is True
    assert res["span_km"] == pytest.approx(55.6, abs=0.5)
    assert res["warn"] is True


def test_place_merge_preview_members_without_geo_are_ignored(tmp_path):
    conn = _place_catalog(tmp_path)
    _insert_place(conn, 1, 1, "Home", 0.0, 0.0, 2)
    _insert_place(conn, 2, 1, None, 0.0, 0.0, 2)
    _attach(conn, 1, 1, "manual"); _attach(conn, 1, 2, "manual")
    _attach(conn, 2, 3, "manual"); _attach(conn, 2, 4, "manual")
    # Every member is a manual attachment with no geo row at all -- nothing
    # for the query to join against, so it must return a clean 0.0 rather
    # than raising or dividing by zero.
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()

    res = queries.place_merge_preview(str(db_path), 1, 2, _WARN_KM)
    assert res["ok"] is True
    assert res["span_km"] == 0.0
    assert res["warn"] is False


def test_place_merge_preview_rejects_same_errors_as_merge(tmp_path):
    conn = _place_catalog(tmp_path, root_ids=(1, 2))
    _insert_place(conn, 1, 1, "Home", 0.0, 0.0, 1)
    _insert_place(conn, 2, 2, "Office", 5.0, 5.0, 1)
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()

    assert "error" in queries.place_merge_preview(str(db_path), 1, 1, _WARN_KM)
    assert "error" in queries.place_merge_preview(str(db_path), 1, 999999, _WARN_KM)
    assert "error" in queries.place_merge_preview(str(db_path), 1, 2, _WARN_KM)
