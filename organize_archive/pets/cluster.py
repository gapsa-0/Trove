"""Conservative within-species pet grouping with durable names."""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Config
from ..db import database as db


@dataclass
class PetClusterStats:
    detections: int = 0
    pets: int = 0
    clustered: int = 0
    unassigned: int = 0
    names_preserved: int = 0


def _clusters(vectors, threshold):
    """Greedy complete-link grouping; every member must match every other."""
    import numpy as np
    groups = []
    for index, vector in enumerate(vectors):
        choices = []
        for group_index, group in enumerate(groups):
            similarities = vectors[group] @ vector
            if float(similarities.min()) >= threshold:
                choices.append((float(similarities.mean()), group_index))
        if choices:
            groups[max(choices)[1]].append(index)
        else:
            groups.append([index])
    return groups


def cluster_pets(conn, cfg: Config, root_id=None) -> PetClusterStats:
    import numpy as np
    stats = PetClusterStats()
    # Pet identities are catalog-global just like persons. Rebuild from every
    # scanned root so opening one archive cannot erase another archive's pets.
    rc, params = "", ()
    rows = conn.execute(
        f"""SELECT a.* FROM animal_detections a JOIN files f ON f.id=a.file_id
            WHERE f.present=1 AND f.hidden=0 AND a.species!='teddy bear'{rc}
            ORDER BY a.species,a.id""", params).fetchall()
    stats.detections = len(rows)
    old_members = {}
    for pet in conn.execute("SELECT id,name FROM pets WHERE name IS NOT NULL"):
        old_members[pet["id"]] = {
            "name": pet["name"],
            "ids": {r[0] for r in conn.execute(
                "SELECT id FROM animal_detections WHERE pet_id=?", (pet["id"],))},
        }
    conn.execute("UPDATE animal_detections SET pet_id=NULL WHERE manual_pet IS NULL")
    conn.execute("DELETE FROM pets")
    now = db.now_iso()
    for species in sorted({row["species"] for row in rows}):
        species_rows = [row for row in rows if row["species"] == species
                        and row["embedding"]]
        if not species_rows:
            continue
        vectors = np.array(
            [np.frombuffer(row["embedding"], "float32") for row in species_rows],
            dtype="float32")
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True) + 1e-9
        for group in _clusters(vectors, cfg.pets_cluster_similarity):
            if len(group) < cfg.pets_min_detections:
                continue
            ids = {species_rows[index]["id"] for index in group}
            best_name, best_overlap = None, 0
            for old in old_members.values():
                overlap = len(ids & old["ids"])
                if overlap > best_overlap:
                    best_name, best_overlap = old["name"], overlap
            centroid = vectors[group].mean(axis=0)
            centroid /= np.linalg.norm(centroid) + 1e-9
            cover = max((species_rows[index] for index in group),
                        key=lambda row: row["det_score"])
            cursor = conn.execute(
                """INSERT INTO pets
                   (name,species,cover_detection_id,detection_count,centroid,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (best_name, species, cover["id"], len(group),
                 centroid.astype("float32").tobytes(), now))
            marks = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE animal_detections SET pet_id=? WHERE id IN ({marks})",
                (cursor.lastrowid, *ids))
            stats.pets += 1
            stats.clustered += len(group)
            stats.names_preserved += int(best_name is not None)
    stats.unassigned = stats.detections - stats.clustered
    conn.commit()
    return stats
