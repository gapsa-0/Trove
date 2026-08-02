"""Durable merges: merge_pets/merge_persons must survive the next automatic
rebuild (cluster_pets deletes+rebuilds `pets` after every detect chunk; a
naive merge would be undone within a minute without the pet_links / face_links
constraint each keys off DETECTION/FACE ids rather than the ephemeral group id)."""

from __future__ import annotations

import factories
import pytest

from organize_archive.config import Config
from organize_archive.db import database as db
from organize_archive.pets import cluster
from organize_archive.services import people_edit, pets

np = pytest.importorskip("numpy")


def _catalog(tmp_path, count):
    conn = factories.make_db(tmp_path)
    for file_id in factories.add_files(conn, count):
        (tmp_path / "photos" / f"{file_id}.jpg").write_bytes(b"fake")
    conn.commit()
    return conn


def _insert_detection(conn, det_id, file_id, species, vector, score):
    factories.add_animal_detection(
        conn,
        file_id,
        detection_id=det_id,
        species=species,
        det_score=score,
        embedding=vector.astype("float32").tobytes(),
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

    merged = pets.merge_pets(str(db_path), pet_ids[0], pet_ids[1])
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

    refused = pets.merge_pets(str(db_path), 1, 2)
    assert "error" in refused
    assert "Fido" in refused["error"] and "Rex" in refused["error"]

    ok = pets.merge_pets(str(db_path), 1, 2, name="Buddy")
    assert ok["ok"] is True
    assert ok["pet"]["name"] == "Buddy"

    check = db.open_readonly(db_path)
    assert check.execute("SELECT name FROM pets").fetchone()[0] == "Buddy"
    check.close()


def _catalog_with_named_persons(tmp_path):
    conn = factories.make_db(tmp_path)
    factories.add_file(conn, file_id=1)
    factories.add_file(conn, file_id=2)
    factories.add_person(conn, name="Ana", person_id=1)
    factories.add_person(conn, name="Beto", person_id=2)
    # Both faces are manually pinned by name, so a merge that doesn't rewrite
    # the losing pin would let faces/cluster.py's _apply_manual_pins recreate
    # the merged-away person on the next recluster. Embeddings just need to be
    # valid float32 blobs -- merge_persons recomputes the centroid from them.
    vec = np.ones(2, dtype="float32").tobytes()
    for face_id, (file_id, person_id, name) in enumerate([(1, 1, "Ana"), (2, 2, "Beto")], 1):
        factories.add_face(
            conn,
            file_id=file_id,
            face_id=face_id,
            box=(0, 0, 1, 1),
            embedding=vec,
            person_id=person_id,
            manual_person=name,
        )
    conn.commit()
    conn.close()
    return tmp_path / "archive.db"


def test_merge_persons_with_explicit_name_succeeds_and_rewrites_pins(tmp_path):
    db_path = _catalog_with_named_persons(tmp_path)

    refused = people_edit.merge_persons(str(db_path), 1, 2)
    assert "error" in refused
    assert "Ana" in refused["error"] and "Beto" in refused["error"]

    ok = people_edit.merge_persons(str(db_path), 1, 2, name="Ana")
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
