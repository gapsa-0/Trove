"""Conservative within-species pet grouping with durable names."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from ..config import Config
from ..db import database as db
from .manual_tags import repair_manual_pet_files


@dataclass
class PetClusterStats:
    detections: int = 0
    pets: int = 0
    clustered: int = 0
    unassigned: int = 0
    names_preserved: int = 0


def _clusters(vectors, threshold):
    """Greedy complete-link grouping; every member must match every other."""
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


def _apply_links(conn, groups, emb_rows):
    """Fold durable "same pet?" / "different pet?" answers (``pet_links``)
    into the automatic groups, mirroring ``faces/cluster.py``'s
    ``_apply_links`` (read that one first). Links are anchored to DETECTION
    ids rather than pet ids because ``cluster_pets`` deletes and rebuilds
    every ``pets`` row after EVERY detect chunk (see ``web/jobs.py``'s
    ``cluster_pets`` call) -- a pet id from the run a link was recorded in no
    longer exists by the time this runs again, but its member detections do.
    Called on the flat, all-species group list *before*
    ``pets_min_detections`` is applied, so a link can union two groups that
    came from *different* per-species passes: a dog once misdetected as a cat
    still needs to rejoin its real identity.

    'same' is a must-link (unions two groups); 'different' is a cannot-link
    written by unmerge_pets to keep an undone merge from silently reforming --
    it BLOCKS a union that would put its two detections' groups together,
    taking precedence over any 'same' chain (mirrors faces/cluster.py's
    cannot-link-wins rule exactly). Like that version, this only affects the
    merge graph among the automatic groups -- it never splits a group the
    automatic pass already formed on its own.

    A link is ignored when either detection no longer exists (its file was
    removed, or it was never embedded) or when both are already in the same
    group. Returns ``groups`` unchanged when there are no links at all."""
    links = conn.execute("SELECT det_a, det_b, kind FROM pet_links").fetchall()
    if not links:
        return groups
    det_to_group: dict[int, int] = {}
    for gi, idxs in enumerate(groups):
        for i in idxs:
            det_to_group[emb_rows[i]["id"]] = gi

    # Small union-find local to this module (faces/cluster.py has its own
    # _DSU, but importing across detector modules for one helper isn't worth
    # the coupling).
    parent = list(range(len(groups)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    same, cannot = [], []
    for link in links:
        ga = det_to_group.get(link["det_a"])
        gb = det_to_group.get(link["det_b"])
        if ga is None or gb is None or ga == gb:
            continue
        (same if link["kind"] == "same" else cannot).append((ga, gb))

    def would_violate(ra, rb):
        return any({find(ga), find(gb)} == {ra, rb} for ga, gb in cannot)

    for ga, gb in same:
        ra, rb = find(ga), find(gb)
        if ra != rb and not would_violate(ra, rb):
            parent[ra] = rb

    merged: dict[int, list[int]] = {}
    for gi, idxs in enumerate(groups):
        merged.setdefault(find(gi), []).extend(idxs)
    return list(merged.values())


def cluster_pets(conn, cfg: Config, root_id=None) -> PetClusterStats:
    import numpy as np

    stats = PetClusterStats()
    # Pet identities are catalog-global just like persons. Rebuild from every
    # scanned root so opening one archive cannot erase another archive's pets.
    rc, params = "", ()
    rows = conn.execute(
        f"""SELECT a.* FROM animal_detections a JOIN files f ON f.id=a.file_id
            WHERE f.present=1 AND f.hidden=0 AND a.species!='teddy bear'{rc}
            ORDER BY a.species,a.id""",
        params,
    ).fetchall()
    stats.detections = len(rows)
    old_members = {}
    for pet in conn.execute("SELECT id,name FROM pets WHERE name IS NOT NULL"):
        old_members[pet["id"]] = {
            "name": pet["name"],
            "ids": {
                r[0]
                for r in conn.execute(
                    "SELECT id FROM animal_detections WHERE pet_id=?", (pet["id"],)
                )
            },
        }
    conn.execute("UPDATE animal_detections SET pet_id=NULL WHERE manual_pet IS NULL")
    conn.execute("DELETE FROM pets")
    now = db.now_iso()

    # pet_files (manual "this pet is in this photo" tags) anchor by NAME
    # because `pets` is rebuilt wholesale on every call, unlike persons this
    # isn't a defensive backstop -- pet ids are GUARANTEED to change every
    # chunk, so every return path below must repair before it commits.

    # One global embedding matrix (rather than per-species, as before) so a
    # pet_links merge (below) can union two groups formed in different
    # per-species passes -- a merge needs both sides addressable by the same
    # position space. Detections without an embedding can't be grouped at all
    # and fall straight into stats.unassigned.
    emb_rows = [row for row in rows if row["embedding"]]
    if not emb_rows:
        stats.unassigned = stats.detections
        repair_manual_pet_files(conn)
        conn.commit()
        return stats
    V = np.array([np.frombuffer(row["embedding"], "float32") for row in emb_rows], dtype="float32")
    V /= np.linalg.norm(V, axis=1, keepdims=True) + 1e-9

    # -- per-species clustering, collected into one flat list of GROUPS -----
    # Each group is a list of GLOBAL positions into emb_rows/V (not positions
    # local to the species slice), so _apply_links below can compare and union
    # groups from different species passes on equal footing.
    groups: list[list[int]] = []
    for species in sorted({row["species"] for row in emb_rows}):
        positions = [i for i, row in enumerate(emb_rows) if row["species"] == species]
        for local_group in _clusters(V[positions], cfg.pets_cluster_similarity):
            groups.append([positions[i] for i in local_group])

    # -- fold in the user's "same pet?" answers, THEN filter/finalize -------
    groups = _apply_links(conn, groups, emb_rows)

    for group in groups:
        if len(group) < cfg.pets_min_detections:
            continue
        ids = {emb_rows[i]["id"] for i in group}
        best_name, best_overlap = None, 0
        for old in old_members.values():
            overlap = len(ids & old["ids"])
            if overlap > best_overlap:
                best_name, best_overlap = old["name"], overlap
        centroid = V[group].mean(axis=0)
        centroid /= np.linalg.norm(centroid) + 1e-9
        cover = max((emb_rows[i] for i in group), key=lambda row: row["det_score"])
        # Species is now the MAJORITY among the merged group's members, not a
        # single per-species pass's label: a pet_links merge can pull in a
        # detection the detector mis-typed (e.g. a dog once read as a cat).
        # Ties go to the best-scoring detection *among the tied species* --
        # not to `cover`, which may itself be the lone high-scoring outlier
        # of a minority species that lost the vote.
        counts = Counter(emb_rows[i]["species"] for i in group)
        top = max(counts.values())
        tied = {sp for sp, c in counts.items() if c == top}
        species = max(
            (emb_rows[i] for i in group if emb_rows[i]["species"] in tied),
            key=lambda row: row["det_score"],
        )["species"]
        cursor = conn.execute(
            """INSERT INTO pets
               (name,species,cover_detection_id,detection_count,centroid,created_at)
               VALUES(?,?,?,?,?,?)""",
            (
                best_name,
                species,
                cover["id"],
                len(group),
                centroid.astype("float32").tobytes(),
                now,
            ),
        )
        marks = ",".join("?" for _ in ids)
        conn.execute(
            f"UPDATE animal_detections SET pet_id=? WHERE id IN ({marks})", (cursor.lastrowid, *ids)
        )
        stats.pets += 1
        stats.clustered += len(group)
        stats.names_preserved += int(best_name is not None)
    stats.unassigned = stats.detections - stats.clustered
    repair_manual_pet_files(conn)
    conn.commit()
    return stats
