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
