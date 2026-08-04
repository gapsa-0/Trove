from __future__ import annotations

import factories
import pytest

from trove.config import Config
from trove.pets import backend, cluster, extract

np = pytest.importorskip("numpy")


def _catalog(tmp_path, count=1):
    conn = factories.make_db(tmp_path)
    for file_id in factories.add_files(conn, count):
        # The rows must have real files behind them: extract() builds a path from
        # root_path/rel_path and hands it to the backend. The fake backend here
        # ignores it, but a fixture whose catalogue disagrees with the disk would
        # make a future test pass for the wrong reason.
        (tmp_path / "photos" / f"{file_id}.jpg").write_bytes(b"fake")
    conn.commit()
    return conn


class _PetBackend:
    def process_path(self, _path):
        return [
            backend.AnimalDetection(
                species="dog",
                x=10,
                y=12,
                w=90,
                h=100,
                score=0.91,
                embedding=np.ones(320, dtype="float32") / np.sqrt(320),
            )
        ]


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
    assert extract.pending_count(conn, model_source=extract.scan_source(cfg)) == 1
    assert extract.extract(conn, cfg, be=_PetBackend()).processed == 1
    assert conn.execute("SELECT COUNT(*) FROM animal_detections").fetchone()[0] == 1
    assert extract.pending_count(conn, model_source="new-model") == 1
    conn.close()


def test_pet_clustering_is_species_separated_and_preserves_names(tmp_path):
    conn = _catalog(tmp_path, count=4)
    vectors = (
        ("cat", np.array([1, 0], dtype="float32")),
        ("cat", np.array([1, 0], dtype="float32")),
        ("dog", np.array([0, 1], dtype="float32")),
        ("dog", np.array([0, 1], dtype="float32")),
    )
    for file_id, (species, vector) in enumerate(vectors, 1):
        factories.add_animal_detection(conn, file_id, species=species, embedding=vector.tobytes())
    conn.commit()
    cfg = Config(pets_cluster_similarity=0.99, pets_min_detections=2)

    first = cluster.cluster_pets(conn, cfg)
    cat = conn.execute("SELECT id FROM pets WHERE species='cat'").fetchone()
    conn.execute("UPDATE pets SET name='Michi' WHERE id=?", (cat["id"],))
    conn.commit()
    second = cluster.cluster_pets(conn, cfg)

    assert first.pets == 2
    assert second.pets == 2
    assert conn.execute("SELECT name FROM pets WHERE species='cat'").fetchone()[0] == "Michi"
    conn.close()
