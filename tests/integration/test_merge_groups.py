"""Durable merges: merge_pets/merge_persons must survive the next automatic
rebuild (cluster_pets deletes+rebuilds `pets` after every detect chunk; a
naive merge would be undone within a minute without the pet_links / face_links
constraint each keys off DETECTION/FACE ids rather than the ephemeral group id)."""

from __future__ import annotations

import pytest

from organize_archive.config import Config
from organize_archive.db import database as db
from organize_archive.gui import queries
from organize_archive.pets import cluster

np = pytest.importorskip("numpy")


def _catalog(tmp_path, count):
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


def _insert_detection(conn, det_id, file_id, species, vector, score):
    conn.execute(
        """INSERT INTO animal_detections
           (id,file_id,species,box_x,box_y,box_w,box_h,det_score,embedding,
            model_source,created_at)
           VALUES(?,?,?,0,0,50,50,?,?,'test','2026-01-01')""",
        (det_id, file_id, species, score, vector.astype("float32").tobytes()),
    )


def test_merge_pets_survives_a_subsequent_full_recluster(tmp_path):
    conn = _catalog(tmp_path, count=4)
    # Two pairs of near-identical embeddings, ORTHOGONAL to each other -- at
    # pets_cluster_similarity=.99 these are two separate identities, not one,
    # so the only thing that can reunite them after a rebuild is a durable link.
    _insert_detection(conn, 1, 1, "dog", np.array([1, 0], dtype="float32"), 0.90)
    _insert_detection(conn, 2, 2, "dog", np.array([1, 0], dtype="float32"), 0.95)
    _insert_detection(conn, 3, 3, "dog", np.array([0, 1], dtype="float32"), 0.90)
    _insert_detection(conn, 4, 4, "dog", np.array([0, 1], dtype="float32"), 0.95)
    conn.commit()
    cfg = Config(pets_cluster_similarity=0.99, pets_min_detections=2)

    first = cluster.cluster_pets(conn, cfg)
    assert first.pets == 2  # two distinct groups before any merge

    pet_ids = [r[0] for r in conn.execute("SELECT id FROM pets ORDER BY id")]
    assert len(pet_ids) == 2
    db_path = tmp_path / "archive.db"
    conn.close()

    merged = queries.merge_pets(str(db_path), pet_ids[0], pet_ids[1])
    assert merged["ok"] is True
    assert merged["pet"]["detections"] == 4

    check = db.connect(db_path)
    assert check.execute("SELECT COUNT(DISTINCT pet_id) FROM animal_detections").fetchone()[0] == 1

    # The important assertion: a subsequent FULL cluster_pets rebuild (which
    # DELETEs and reconstructs every `pets` row from scratch) must not
    # re-split these back into two, because the durable pet_links 'same'
    # constraint recorded by merge_pets is anchored to detection ids that
    # survive the rebuild.
    second = cluster.cluster_pets(check, cfg)
    assert second.pets == 1
    assert check.execute("SELECT COUNT(DISTINCT pet_id) FROM animal_detections").fetchone()[0] == 1
    assert check.execute("SELECT detection_count FROM pets").fetchone()[0] == 4
    check.close()


def test_merge_pets_both_named_differently_requires_explicit_name(tmp_path):
    conn = _catalog(tmp_path, count=2)
    _insert_detection(conn, 1, 1, "dog", np.array([1, 0], dtype="float32"), 0.9)
    _insert_detection(conn, 2, 2, "dog", np.array([1, 0], dtype="float32"), 0.9)
    now = db.now_iso()
    conn.execute(
        "INSERT INTO pets(id,name,species,cover_detection_id,detection_count,"
        "created_at) VALUES(1,'Fido','dog',1,1,?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO pets(id,name,species,cover_detection_id,detection_count,"
        "created_at) VALUES(2,'Rex','dog',2,1,?)",
        (now,),
    )
    conn.execute("UPDATE animal_detections SET pet_id=1 WHERE id=1")
    conn.execute("UPDATE animal_detections SET pet_id=2 WHERE id=2")
    conn.commit()
    db_path = tmp_path / "archive.db"
    conn.close()

    refused = queries.merge_pets(str(db_path), 1, 2)
    assert "error" in refused
    assert "Fido" in refused["error"] and "Rex" in refused["error"]

    ok = queries.merge_pets(str(db_path), 1, 2, name="Buddy")
    assert ok["ok"] is True
    assert ok["pet"]["name"] == "Buddy"

    check = db.open_readonly(db_path)
    assert check.execute("SELECT name FROM pets").fetchone()[0] == "Buddy"
    check.close()


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
            (file_id, f"{file_id}.jpg"),
        )
    now = db.now_iso()
    conn.execute("INSERT INTO persons(id,name,face_count,created_at) VALUES(1,'Ana',1,?)", (now,))
    conn.execute("INSERT INTO persons(id,name,face_count,created_at) VALUES(2,'Beto',1,?)", (now,))
    # Both faces are manually pinned by name, so a merge that doesn't rewrite
    # the losing pin would let faces/cluster.py's _apply_manual_pins recreate
    # the merged-away person on the next recluster. Embeddings just need to be
    # valid float32 blobs -- merge_persons recomputes the centroid from them.
    vec = np.ones(2, dtype="float32").tobytes()
    conn.execute(
        """INSERT INTO faces(id,file_id,box_x,box_y,box_w,box_h,embedding,
                             person_id,manual_person,created_at)
           VALUES(1,1,0,0,1,1,?,1,'Ana',?)""",
        (vec, now),
    )
    conn.execute(
        """INSERT INTO faces(id,file_id,box_x,box_y,box_w,box_h,embedding,
                             person_id,manual_person,created_at)
           VALUES(2,2,0,0,1,1,?,2,'Beto',?)""",
        (vec, now),
    )
    conn.commit()
    conn.close()
    return db_path


def test_merge_persons_with_explicit_name_succeeds_and_rewrites_pins(tmp_path):
    db_path = _catalog_with_named_persons(tmp_path)

    refused = queries.merge_persons(str(db_path), 1, 2)
    assert "error" in refused
    assert "Ana" in refused["error"] and "Beto" in refused["error"]

    ok = queries.merge_persons(str(db_path), 1, 2, name="Ana")
    assert ok["ok"] is True
    assert ok["person"]["name"] == "Ana"
    assert ok["person"]["face_count"] == 2

    check = db.open_readonly(db_path)
    survivor_id = ok["person"]["id"]
    pins = {
        r[0]
        for r in check.execute("SELECT manual_person FROM faces WHERE person_id=?", (survivor_id,))
    }
    # Both faces -- including the one pinned to the LOSING name "Beto" -- must
    # now be pinned to the surviving name, or _apply_manual_pins would
    # resurrect "Beto" as a new person on the next recluster.
    assert pins == {"Ana"}
    assert check.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 1
    check.close()
