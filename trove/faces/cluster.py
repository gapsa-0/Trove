"""Turn the faces table into the persons table: read, cluster, rebuild, commit.

The clustering *argument* -- why two passes, why a mutual k-NN graph, why
average linkage -- lives in ``passes.py``, and the neighbour search it stands on
in ``knn.py``. Neither of those knows what a person is. This module is the half
that does: which faces are eligible, what a rebuild destroys, and what has to
survive it.

Three things must survive the DELETE/rebuild below, and each has its own
mechanism here:

* **Names**, carried across by face-id overlap (``_carry_names``) -- one name to
  one cluster, greedily by best overlap.
* **Manual pins**, re-applied after clustering (``_apply_manual_pins``), which is
  what makes a user's "move this face to Mari" outlive the rebuild. Anchored by
  NAME, not id -- see ADR 0008.
* **Review answers**, folded in as must-link/cannot-link constraints
  (``_apply_links``) anchored to face ids, which are stable across a rebuild.

Idempotent. Only non-hidden files are clustered, so byte-identical takeout
duplicates don't inflate density. LOW_QUALITY faces never enter either pass; a
NULL tier (a row from before the quality gate existed) reads as BORDERLINE.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from ..config import Config
from ..db import database as db
from ..progress import Progress
from .knn import DSU
from .manual_tags import repair_manual_person_files
from .passes import BorderAssigner, CoreBuilder

if TYPE_CHECKING:
    # numpy is optional; the functions that run it import it themselves.
    import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class ClusterStats:
    people: int = 0  # clusters kept as a person
    faces: int = 0  # faces considered (non-hidden, not LOW_QUALITY)
    clustered: int = 0  # faces assigned to a person
    noise: int = 0  # faces left unassigned (singletons / sub-min clusters)
    named: int = 0  # people that inherited a name from a prior run
    fragments: int = 0  # pass-1 multi-face fragments fed to the merge
    high: int = 0  # HIGH-tier faces (eligible to seed cores)
    cores: int = 0  # cores built by pass 1
    borderline: int = 0  # BORDERLINE faces offered to pass 2
    border_assigned: int = 0  # of those, attached to a core
    low_quality_excluded: int = 0  # faces the FIQA gate kept out entirely


def _apply_links(
    conn: sqlite3.Connection, cluster_list: list[list[int]], face_ids: Sequence[int]
) -> list[list[int]]:
    """Fold the user's "same person?" answers into the automatic clusters.

    Constraints live in ``face_links`` anchored to face ids (stable across the
    rebuild). A 'same' link unions the two faces' clusters (fixing an under-merge
    like a loose satellite); a 'different' link is a cannot-link that BLOCKS a
    union which would put the two together (cannot-link wins over must-link, so a
    chain of 'same' answers can never override an explicit 'different'). Returns
    the reconciled cluster list. Only affects the merge graph among the automatic
    clusters — it never splits a cluster the automatic pass already formed
    (a rare 'different' inside one auto-cluster is left as-is; the auto pass is
    conservative enough that this effectively never happens)."""
    links = conn.execute("SELECT face_a, face_b, kind FROM face_links").fetchall()
    if not links:
        return cluster_list
    fid2c: dict[int, int] = {}
    for ci, idxs in enumerate(cluster_list):
        for i in idxs:
            fid2c[face_ids[i]] = ci
    same: list[tuple[int, int]] = []
    cannot: list[tuple[int, int]] = []
    for lk in links:
        ca, cb = fid2c.get(lk["face_a"]), fid2c.get(lk["face_b"])
        if ca is None or cb is None or ca == cb:
            continue  # a face is noise, or already together
        (same if lk["kind"] == "same" else cannot).append((ca, cb))
    if not same:
        return cluster_list
    dsu = DSU(len(cluster_list))

    def would_violate(ra: int, rb: int) -> bool:
        return any({dsu.find(ca), dsu.find(cb)} == {ra, rb} for ca, cb in cannot)

    for ca, cb in same:
        ra, rb = dsu.find(ca), dsu.find(cb)
        if ra != rb and not would_violate(ra, rb):
            dsu.union(ca, cb)
    merged: dict[int, list[int]] = {}
    for ci in range(len(cluster_list)):
        merged.setdefault(dsu.find(ci), []).extend(cluster_list[ci])
    return list(merged.values())


def _apply_manual_pins(conn: sqlite3.Connection, now: str) -> set[int]:
    """Force every manually-pinned face into the person that carries its pinned
    NAME, overriding whatever cluster its embedding fell into. Creates the person
    if the clustering pass produced none with that name. This is what makes a
    user's "move this face to Mari" survive the DELETE/rebuild above.

    Returns the set of person ids whose membership changed (the pinned-to person
    plus the cluster each moved face left), so only those need a stats refresh —
    the clustering already set correct stats for every untouched person."""
    pins = conn.execute(
        "SELECT id, person_id, manual_person FROM faces "
        "WHERE manual_person IS NOT NULL AND manual_person != ''"
    ).fetchall()
    if not pins:
        return set()
    name_to_pid: dict[str, int] = {}
    for p in conn.execute("SELECT id, name FROM persons WHERE name IS NOT NULL AND name != ''"):
        name_to_pid.setdefault(p["name"], p["id"])
    affected: set[int] = set()
    for f in pins:
        name, cur_pid = f["manual_person"], f["person_id"]
        pid = name_to_pid.get(name)
        if pid is None:
            cur = conn.execute(
                "INSERT INTO persons(name, cover_face_id, face_count, created_at) VALUES(?,?,0,?)",
                (name, f["id"], now),
            )
            # An INSERT that didn't raise always sets lastrowid; see
            # db.database.get_or_create_root for why typeshed still widens it.
            pid = cast(int, cur.lastrowid)
            name_to_pid[name] = pid
        if cur_pid != pid:
            conn.execute("UPDATE faces SET person_id=? WHERE id=?", (pid, f["id"]))
            affected.add(pid)
            if cur_pid is not None:
                affected.add(cur_pid)
    return affected


def _refresh_person_stats(conn: sqlite3.Connection, pids: Iterable[int]) -> None:
    """Recompute face_count + a sharp cover (highest det_score, non-hidden) for the
    given persons, dropping any left empty. Scoped to the handful of pin-affected
    persons so it's a short write — cluster_faces runs this inside its rebuild
    transaction, which a colliding GUI write must wait behind."""
    for pid in pids:
        if not conn.execute("SELECT 1 FROM faces WHERE person_id=? LIMIT 1", (pid,)).fetchone():
            conn.execute("DELETE FROM persons WHERE id=?", (pid,))
            continue
        cnt = conn.execute(
            "SELECT COUNT(*) FROM faces fa JOIN files f ON f.id=fa.file_id "
            "WHERE fa.person_id=? AND f.hidden=0",
            (pid,),
        ).fetchone()[0]
        cover = conn.execute(
            "SELECT fa.id FROM faces fa JOIN files f ON f.id=fa.file_id "
            "WHERE fa.person_id=? AND f.hidden=0 "
            # The cover is the face shown on the person's card, so it must never
            # be one the quality gate rejected.
            "AND COALESCE(fa.quality_tier,'BORDERLINE') != 'LOW_QUALITY' "
            "ORDER BY fa.det_score DESC LIMIT 1",
            (pid,),
        ).fetchone()
        conn.execute(
            "UPDATE persons SET face_count=?, cover_face_id=? WHERE id=?",
            (cnt, cover["id"] if cover else None, pid),
        )


def _reapply_person_hides(conn: sqlite3.Connection) -> None:
    """Re-hide the clusters the user hid, after the rebuild above cleared the flag.

    `persons.hidden` is convenient for the read queries but cannot be durable:
    every row carrying it was just deleted. `person_hides` is the record that
    survives, anchored to a face id, and this is what turns it back into the
    flag. Whichever cluster that face landed in this time is the one that gets
    hidden -- which is the right answer even when clustering has since split or
    fused the group, for the same reason manual pins follow their face.
    """
    conn.execute(
        """UPDATE persons SET hidden=1 WHERE id IN (
               SELECT fa.person_id FROM person_hides h
               JOIN faces fa ON fa.id = h.rep_face_id
               WHERE fa.person_id IS NOT NULL)"""
    )


def _finalize(
    conn: sqlite3.Connection, stats: ClusterStats, now: str, progress: Progress | None
) -> ClusterStats:
    """Re-apply manual pins, tidy affected person stats, commit. Every return path
    of cluster_faces goes through here so pins are honored even when the automatic
    clustering found nothing.

    Also repairs person_files (manual "this person is in this photo" tags for
    media with no detected face at all): cluster_faces just DELETEd and
    rebuilt every `persons` row above, so any manual tag whose person_id no
    longer carries its anchored name needs re-pointing at whichever person
    (if any) carries it now. This is the single choke point every return path
    goes through, so it's the one place that needs the call."""
    repair_manual_person_files(conn)
    _refresh_person_stats(conn, _apply_manual_pins(conn, now))
    _reapply_person_hides(conn)
    stats.people = conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
    conn.commit()
    if progress is not None:
        progress.update(stats.faces, 0, f"{stats.people} people")
    return stats


def _cannot_pairs(conn: sqlite3.Connection) -> set[frozenset[int]]:
    return {
        frozenset((r["face_a"], r["face_b"]))
        for r in conn.execute("SELECT face_a, face_b FROM face_links WHERE kind != 'same'")
    }


def _load_faces(conn: sqlite3.Connection, stats: ClusterStats) -> list[sqlite3.Row]:
    """Every clusterable face, plus the count of those the quality gate excluded.

    LOW_QUALITY faces are excluded here and nowhere else needs to know: this is
    the single gate between the FIQA verdict and clustering.
    """
    rows = conn.execute(
        """SELECT fa.id, fa.det_score, fa.embedding,
                  COALESCE(fa.quality_tier, 'BORDERLINE') AS tier
           FROM faces fa JOIN files f ON f.id = fa.file_id
           WHERE f.hidden = 0 AND COALESCE(fa.not_person, 0) = 0
                 AND COALESCE(fa.quality_tier, 'BORDERLINE') != 'LOW_QUALITY'
           ORDER BY fa.id"""
    ).fetchall()
    stats.faces = len(rows)
    stats.low_quality_excluded = conn.execute(
        """SELECT COUNT(*) FROM faces fa JOIN files f ON f.id = fa.file_id
           WHERE f.hidden = 0 AND COALESCE(fa.not_person, 0) = 0
                 AND fa.quality_tier = 'LOW_QUALITY'"""
    ).fetchone()[0]
    return rows


def _remember_named(conn: sqlite3.Connection) -> list[tuple[str, set[int]]]:
    """Each named person's face-id set, so the name can be carried across rebuild."""
    old_named: list[tuple[str, set]] = []
    for pid, name in conn.execute(
        "SELECT id, name FROM persons WHERE name IS NOT NULL AND name != ''"
    ):
        fids = {r["id"] for r in conn.execute("SELECT id FROM faces WHERE person_id=?", (pid,))}
        if fids:
            old_named.append((name, fids))
    return old_named


def _matrix(rows: list[sqlite3.Row]) -> np.ndarray:
    """L2-normalized embedding matrix for ``rows``, in row order.

    Embedding dim is inferred from the stored blob, so a backend switch needs no
    code change here — only a re-extract.
    """
    import numpy as np

    dim = len(rows[0]["embedding"]) // 4
    X = np.empty((len(rows), dim), dtype="float32")
    for i, r in enumerate(rows):
        X[i] = np.frombuffer(r["embedding"], dtype="float32")
    X /= np.linalg.norm(X, axis=1, keepdims=True) + 1e-9
    return X


def _build_cores(
    X: np.ndarray,
    rows: list[sqlite3.Row],
    cfg: Config,
    stats: ClusterStats,
    progress: Progress | None,
) -> tuple[list[list[int]], bool]:
    """Pass 1, plus the fallback for an archive that has no quality tiers yet.

    If every face reads as BORDERLINE (a database extracted before the FIQA gate)
    there would be nothing to seed cores with, so seeding falls back to
    everything: worse noise resistance, but a working result, and the next
    extract fills the tiers in. Returns ``(cores, seeded_from_all)`` with cores
    as global row indices.
    """
    import numpy as np

    high_idx = [i for i, r in enumerate(rows) if r["tier"] == "HIGH"]
    stats.high = len(high_idx)
    stats.borderline = len(rows) - len(high_idx)

    seeded_from_all = len(high_idx) < cfg.faces_min_faces
    seed_idx = list(range(len(rows))) if seeded_from_all else high_idx
    builder = CoreBuilder(cfg)
    local_cores = builder.build(X[np.asarray(seed_idx, dtype=np.int64)], progress=progress)
    stats.fragments = builder.fragments
    cores = [[seed_idx[i] for i in core] for core in local_cores]
    stats.cores = len(cores)
    return cores, seeded_from_all


def _carry_names(fsets: list[set[int]], old_named: list[tuple[str, set[int]]]) -> dict[int, str]:
    """Give each previously-used name to the one new cluster it best explains.

    Sort all (name, cluster) overlaps descending and assign greedily, each name
    used once and each cluster named once. This one-to-one rule is what stops a
    name from spreading — e.g. an old megacluster named "Noelia" holding many
    people's faces lands "Noelia" on just the one cluster that inherited the most
    of them, leaving the rest unnamed.

    Do NOT also gate on the name covering a *majority* of the new cluster: when a
    person who was split is reunited (average linkage merging their sub-clusters),
    the old name sat on only one of those sub-clusters, so it is a minority of the
    bigger merged cluster — a majority gate would then wrongly DROP the name. The
    small floor below only rejects incidental 1-2 face overlaps.
    """
    triples = []  # (overlap, name, cluster_idx)
    for ci, fset in enumerate(fsets):
        for cand_name, cand_fids in old_named:
            ov = len(fset & cand_fids)
            if ov >= 3:  # ignore incidental tiny overlaps
                triples.append((ov, cand_name, ci))
    triples.sort(reverse=True)
    name_of: dict[int, str] = {}
    used: set[str] = set()
    for _ov, cand_name, ci in triples:
        if cand_name in used or ci in name_of:
            continue
        name_of[ci] = cand_name
        used.add(cand_name)
    return name_of


def _write_people(
    conn: sqlite3.Connection,
    kept: list[list[int]],
    X: np.ndarray,
    face_ids: Sequence[int],
    scores: Sequence[float],
    name_of: dict[int, str],
    now: str,
    stats: ClusterStats,
) -> None:
    """Insert one persons row per kept cluster and point its faces at it."""
    import numpy as np

    assign: list[tuple[int, int]] = []
    for ci, idxs in enumerate(kept):
        name = name_of.get(ci)
        cover = max(idxs, key=lambda i: scores[i])
        cvec = X[idxs].mean(0)
        cvec = (cvec / (np.linalg.norm(cvec) + 1e-9)).astype("float32")
        cur = conn.execute(
            """INSERT INTO persons (name, cover_face_id, face_count, centroid, created_at)
               VALUES (?,?,?,?,?)""",
            (name, face_ids[cover], len(idxs), cvec.tobytes(), now),
        )
        # An INSERT that didn't raise always sets lastrowid; see
        # db.database.get_or_create_root for why typeshed still widens it.
        pid = cast(int, cur.lastrowid)
        assign.extend((pid, face_ids[i]) for i in idxs)
        stats.people += 1
        stats.clustered += len(idxs)
        if name:
            stats.named += 1
    stats.noise = stats.faces - stats.clustered
    conn.executemany("UPDATE faces SET person_id=? WHERE id=?", assign)


def cluster_faces(
    conn: sqlite3.Connection, cfg: Config, progress: Progress | None = None
) -> ClusterStats:
    db.init_db(conn)
    stats = ClusterStats()
    now = db.now_iso()

    rows = _load_faces(conn, stats)
    if progress is not None:
        progress.total = stats.faces or 1
    old_named = _remember_named(conn)

    conn.execute("UPDATE faces SET person_id=NULL")
    conn.execute("DELETE FROM persons")
    if stats.faces < cfg.faces_min_faces:
        return _finalize(conn, stats, now, progress)

    face_ids = [r["id"] for r in rows]
    scores = [r["det_score"] or 0.0 for r in rows]
    X = _matrix(rows)

    cores, seeded_from_all = _build_cores(X, rows, cfg, stats, progress)
    if not cores:
        return _finalize(conn, stats, now, progress)

    # -- pass 2: attach borderline faces to a core, or leave them as noise -
    border_idx = [i for i, r in enumerate(rows) if r["tier"] != "HIGH"]
    if not seeded_from_all and border_idx:
        assigned = BorderAssigner(cfg).assign(
            X, cores, border_idx, cannot=_cannot_pairs(conn), face_ids=face_ids
        )
        for global_i, ci in assigned.items():
            cores[ci].append(global_i)
        stats.border_assigned = len(assigned)
        if progress is not None:
            progress.update(stats.faces, 0, "attaching borderline faces…")

    # -- pass 3: fold in the user's "same person?" answers ----------------
    cluster_list = _apply_links(conn, cores, face_ids)
    kept = [idxs for idxs in cluster_list if len(idxs) >= cfg.faces_min_faces]
    fsets = [{face_ids[i] for i in idxs} for idxs in kept]

    _write_people(conn, kept, X, face_ids, scores, _carry_names(fsets, old_named), now, stats)
    return _finalize(conn, stats, now, progress)
