from __future__ import annotations

from types import SimpleNamespace

import pytest

from organize_archive.config import Config
from organize_archive.db import database as db
from organize_archive.faces import backend as face_backend
from organize_archive.faces import extract as face_extract
from organize_archive.gui import queries
from organize_archive.pets import backend, cluster, extract


np = pytest.importorskip("numpy")


def _catalog(tmp_path, count=1):
    root = tmp_path / "photos"
    root.mkdir()
    conn = db.connect(tmp_path / "archive.db")
    db.init_db(conn)
    conn.execute(
        "INSERT INTO roots(id,path,added_at) VALUES(1,?,'2026-01-01')",
        (str(root),))
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


class _PetBackend:
    def process_path(self, _path):
        return [backend.AnimalDetection(
            species="dog", x=10, y=12, w=90, h=100, score=.91,
            embedding=np.ones(320, dtype="float32") / np.sqrt(320))]


def test_pet_extraction_is_resumable_and_persists_provenance(tmp_path, monkeypatch):
    conn = _catalog(tmp_path)
    monkeypatch.setattr(backend, "available", lambda: True)
    cfg = Config(pets_model_version="test-detector-v1")

    first = extract.extract(conn, cfg, be=_PetBackend())
    second = extract.extract(conn, cfg, be=_PetBackend())

    animal = conn.execute("SELECT * FROM animal_detections").fetchone()
    scan = conn.execute("SELECT * FROM pet_scan").fetchone()
    assert first.processed == 1
    assert first.animals == 1
    assert second.processed == 0
    assert animal["species"] == "dog"
    assert animal["model_source"].startswith("test-detector-v1;")
    assert scan["n_animals"] == 1
    conn.execute("UPDATE files SET sha256='changed' WHERE id=1")
    conn.commit()
    assert extract.pending_count(
        conn, model_source=extract.scan_source(cfg)) == 1
    assert extract.extract(conn, cfg, be=_PetBackend()).processed == 1
    assert conn.execute("SELECT COUNT(*) FROM animal_detections").fetchone()[0] == 1
    assert extract.pending_count(conn, model_source="new-model") == 1
    conn.close()


def test_pet_clustering_is_species_separated_and_preserves_names(tmp_path):
    conn = _catalog(tmp_path, count=4)
    now = db.now_iso()
    vectors = (
        ("cat", np.array([1, 0], dtype="float32")),
        ("cat", np.array([1, 0], dtype="float32")),
        ("dog", np.array([0, 1], dtype="float32")),
        ("dog", np.array([0, 1], dtype="float32")),
    )
    for file_id, (species, vector) in enumerate(vectors, 1):
        conn.execute(
            """INSERT INTO animal_detections
               (file_id,species,box_x,box_y,box_w,box_h,det_score,embedding,
                model_source,created_at)
               VALUES(?,?,0,0,50,50,.9,?,'test',?)""",
            (file_id, species, vector.tobytes(), now))
    conn.commit()
    cfg = Config(pets_cluster_similarity=.99, pets_min_detections=2)

    first = cluster.cluster_pets(conn, cfg)
    cat = conn.execute("SELECT id FROM pets WHERE species='cat'").fetchone()
    conn.execute("UPDATE pets SET name='Michi' WHERE id=?", (cat["id"],))
    conn.commit()
    second = cluster.cluster_pets(conn, cfg)

    assert first.pets == 2
    assert second.pets == 2
    assert conn.execute(
        "SELECT name FROM pets WHERE species='cat'").fetchone()[0] == "Michi"
    conn.close()


class _FaceBackend:
    def process_path_report(self, _path):
        face = SimpleNamespace(
            x=20, y=20, w=30, h=30, score=.95,
            focus_score=100.0, brightness=120.0, extreme_fraction=.01,
            clipped_fraction=0.0, quality_score=.8, quality_source="test",
            embedding=np.array([1.0, 0.0], dtype="float32"))
        return face_backend.DetectionReport(faces=[face], candidates=1)


def test_animal_overlap_filters_face_but_manual_review_can_restore_it(
        tmp_path, monkeypatch):
    conn = _catalog(tmp_path)
    conn.execute(
        """INSERT INTO animal_detections
           (file_id,species,box_x,box_y,box_w,box_h,det_score,embedding,
            model_source,created_at)
           VALUES(1,'dog',0,0,100,100,.9,X'00','test','2026-01-01')""")
    conn.commit()
    monkeypatch.setattr(face_backend, "available", lambda: True)
    cfg = Config(pets_face_overlap=.6)

    stats = face_extract.extract(conn, cfg, be=_FaceBackend())

    assert stats.faces_found == 0
    assert stats.rejected_nonhuman == 1
    candidate = conn.execute("SELECT * FROM nonhuman_detections").fetchone()
    assert candidate["kind"] == "animal"
    assert conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0] == 0
    conn.close()

    result = queries.review_nonhuman(
        str(tmp_path / "archive.db"), candidate["id"], "human")
    assert result["ok"]
    check = db.open_readonly(tmp_path / "archive.db")
    assert check.execute("SELECT COUNT(*) FROM faces").fetchone()[0] == 1
    assert check.execute(
        "SELECT rejected_nonhuman FROM face_scan").fetchone()[0] == 0
    check.close()

    # A detector/config rescan of the same source must preserve this correction.
    write = db.connect(tmp_path / "archive.db")
    write.execute(
        """INSERT OR REPLACE INTO pet_scan
           (file_id,n_animals,source_sha256,model_source,scanned_at)
           VALUES(1,1,NULL,'old-model','2026-01-01')""")
    write.commit()
    monkeypatch.setattr(backend, "available", lambda: True)
    extract.extract(write, cfg, be=_PetBackend())
    assert write.execute(
        "SELECT review_status FROM nonhuman_detections").fetchone()[0] == "human"
    assert write.execute("SELECT not_person FROM faces").fetchone()[0] == 0
    write.close()


@pytest.mark.skipif(backend.cv2 is None, reason="OpenCV is optional")
def test_crop_descriptor_is_normalized_and_fixed_width():
    crop = np.full((80, 50, 3), (30, 80, 160), dtype="uint8")
    vector = backend.crop_descriptor(crop)
    assert vector.shape == (320,)
    assert np.linalg.norm(vector) == pytest.approx(1.0)
