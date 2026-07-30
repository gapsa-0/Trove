"""Cluster face embeddings into people — 2-pass core-expansion, noise-resistant.

Structure (the outer shape; the inner stages are described further down):

  **Pass 1 — cores.** Only HIGH-tier faces (faces/fiqa.py) take part. They are
  clustered into "cores" by the three chaining-resistant stages below. Because
  every participant is a good face, a core is pure by construction.

  **Pass 2 — border assignment.** BORDERLINE faces are then matched against the
  finished cores by k-NN and attached to one if they are close enough, otherwise
  left as noise. A borderline face can *join* a core; it can never *create* one
  and never *merge two*. That single restriction is what defuses bridge faces:
  the damage a bridge does is always a merge, and a borderline face is not
  allowed to cause one.

  LOW_QUALITY faces never enter either pass.

Why the quality split matters: a bare similarity graph over *all* faces is
single-linkage, so one spurious bridge face (blurry / profile / a false-positive
detection weakly similar to two different people) fuses both their components.
On this archive that percolated into ONE blob holding ~40% of all faces
(measured intra-blob cosine ~0.15, i.e. pure noise), and raising the threshold
didn't help: even at a value high enough to start splitting true identities the
biggest component was still ~12%.

The three stages inside pass 1, none of which is a bare threshold:

  1. **Over-cluster into small, pure fragments via a MUTUAL k-NN graph.** Link
     two faces only when each is among the other's ``faces_knn_k`` most-similar
     faces *and* their cosine similarity is >= ``faces_core_link_sim``. Capping
     every face to its k best neighbours and requiring reciprocity strips the
     hub/bridge edges that single-linkage rides on, so components stay small and
     pure; singletons are dropped rather than allowed to bridge.
  2. **Merge fragment centroids** with *average*-linkage agglomerative clustering
     at ``faces_merge_sim`` (mean cross-pair cosine). Complete linkage was tried
     first and rejected: it merges two groups only when *every* cross pair is
     close, so a high-variance identity (same person young vs old, many poses)
     never coalesces and the most-photographed person split into ~30 clusters.
     Average linkage keys on the mean, tolerating that spread; it can't chain
     here because stage 1 already stripped the bridges and — measured on this
     archive — different people's centroids are <=~0.30 cosine while one person's
     sub-clusters are ~0.75-0.97, a wide gap the ~0.40 threshold sits inside.
  3. **Centroid merge** (``faces_centroid_merge_sim``): re-merge whole clusters
     whose normalized-centroid cosine is high. The mean-cross-pair metric of
     stage 2 is dragged down by a loose cluster's spread, so a tight and a loose
     cluster of the *same* person (centroids ~0.62) can be left as two people.
     Comparing centroid *directions* divides out that spread and rejoins them,
     with a wide safety margin above the ~0.30 where different people sit.

Stages 2 and 3 are what make pass 1's strict ``faces_core_link_sim`` (~0.75)
safe: strictness there produces *more, smaller* fragments, and these two stages
put each person's fragments back together.

All k-NN search runs through FAISS ``IndexFlatIP`` — exact inner product, which on
L2-normalized vectors *is* cosine, so it is a speed change and not an accuracy
change (``_knn_search`` keeps the original blocked-GEMM path and the two are
asserted equivalent in the tests).

Idempotent rebuild; user-assigned names carried over by face-id overlap (like
geo/clusters.py). Only non-hidden files are clustered, so byte-identical takeout
duplicates don't inflate density.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..config import Config
from ..db import database as db
from .manual_tags import repair_manual_person_files

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


class _DSU:
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _faiss():
    """The faiss module, or None if it isn't installed.

    Optional exactly like every other heavy dependency here: without it the
    blocked-GEMM path below still produces identical results, only slower.
    """
    try:
        import faiss

        return faiss
    except Exception:  # pragma: no cover - optional dep
        # Broad on purpose: a half-installed native faiss build can fail in more
        # ways than ImportError (e.g. OSError from a missing shared library, or a
        # RuntimeError from an ABI mismatch). The blocked-GEMM fallback below is
        # always correct, so any failure here should degrade, never crash.
        logger.debug("faiss unavailable; falling back to the blocked-GEMM path", exc_info=True)
        return None


def _knn_search(X, k: int, block: int = 1024, progress=None, use_faiss: bool = True):
    """Top-``k`` neighbours of every row of ``X``, self-match excluded.

    Returns ``(nbr, nbs)`` — indices and cosine similarities, each ``(n, k)``,
    sorted most-similar-first. ``X`` must be L2-normalized, which makes an inner
    product a cosine.

    Two interchangeable implementations. FAISS ``IndexFlatIP`` is a *flat* index:
    it scans every vector, so it is exact, not approximate — the win is a tuned
    BLAS/SIMD kernel, not a shortcut. The blocked-GEMM path is kept both as the
    no-faiss fallback and as the reference the tests assert FAISS against, so a
    change of engine can never quietly become a change of clustering.
    """
    import numpy as np

    n = len(X)
    k = max(1, min(k, n - 1))  # can't have more neighbours than faces
    faiss = _faiss() if use_faiss else None

    if faiss is not None:
        index = faiss.IndexFlatIP(X.shape[1])
        index.add(np.ascontiguousarray(X))
        # Ask for k+1 because each vector retrieves itself; with duplicate
        # vectors the self-hit is not reliably first, so drop it by identity
        # rather than by position.
        want = min(n, k + 1)
        sims, idx = index.search(np.ascontiguousarray(X), want)
        keep = idx != np.arange(n)[:, None]
        # Stable argsort of ~keep moves the kept columns to the front while
        # preserving FAISS's descending-similarity order among them.
        order = np.argsort(~keep, axis=1, kind="stable")[:, :k]
        nbr = np.take_along_axis(idx, order, axis=1).astype(np.int32)
        nbs = np.take_along_axis(sims, order, axis=1).astype(np.float32)
        if progress is not None:
            progress.update(n, 0, "grouping faces…")
        return nbr, nbs

    # Per-face top-k neighbour indices + similarities (blocked, argpartition is
    # O(n) per row vs a full sort). Self-match is masked out before selection.
    nbr = np.full((n, k), -1, dtype=np.int32)
    nbs = np.zeros((n, k), dtype=np.float32)
    for i0 in range(0, n, block):
        i1 = min(n, i0 + block)
        sims = X[i0:i1] @ X.T  # (b, n)
        sims[np.arange(i1 - i0), np.arange(i0, i1)] = -1.0
        idx = np.argpartition(-sims, k, axis=1)[:, :k]
        part = np.take_along_axis(sims, idx, axis=1)
        order = np.argsort(-part, axis=1)
        nbr[i0:i1] = np.take_along_axis(idx, order, axis=1)
        nbs[i0:i1] = np.take_along_axis(part, order, axis=1)
        del sims, idx, part, order  # free the (b, n) block promptly
        if progress is not None:
            progress.update(i1, 0, "grouping faces…")
    return nbr, nbs


def _mutual_knn(X, k: int, floor: float, block: int = 1024, progress=None):
    """Pass-1 over-cluster via a MUTUAL k-NN graph (see module docstring).

    Union two faces only when each is among the other's ``k`` most-similar faces
    *and* their cosine similarity is >= ``floor``. Returns a ``_DSU`` whose
    components are the fragments. This replaces the old "union every pair >=
    threshold", which was single-linkage and chained distinct people into one
    giant blob.
    """
    import numpy as np

    n = len(X)
    k = max(1, min(k, n - 1))
    nbr, nbs = _knn_search(X, k, block=block, progress=progress)
    k = nbr.shape[1]

    # Directed edges i->j (j among i's k best, similarity >= floor), then keep
    # only reciprocated pairs: pack each undirected pair as min*n+max and require
    # it to appear from both endpoints (count == 2).
    src = np.repeat(np.arange(n), k)
    dst = nbr.reshape(-1)
    keep = (dst >= 0) & (nbs.reshape(-1) >= floor)
    src, dst = src[keep], dst[keep].astype(np.int64)
    a = np.minimum(src, dst)
    b = np.maximum(src, dst)
    key = a * n + b
    uniq, cnt = np.unique(key, return_counts=True)
    mutual = uniq[cnt >= 2]

    dsu = _DSU(n)
    for kk in mutual.tolist():
        dsu.union(kk // n, kk % n)
    return dsu


def _apply_links(conn, cluster_list, face_ids):
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
    fid2c = {}
    for ci, idxs in enumerate(cluster_list):
        for i in idxs:
            fid2c[face_ids[i]] = ci
    same, cannot = [], []
    for lk in links:
        ca, cb = fid2c.get(lk["face_a"]), fid2c.get(lk["face_b"])
        if ca is None or cb is None or ca == cb:
            continue  # a face is noise, or already together
        (same if lk["kind"] == "same" else cannot).append((ca, cb))
    if not same:
        return cluster_list
    dsu = _DSU(len(cluster_list))

    def would_violate(ra, rb):
        return any({dsu.find(ca), dsu.find(cb)} == {ra, rb} for ca, cb in cannot)

    for ca, cb in same:
        ra, rb = dsu.find(ca), dsu.find(cb)
        if ra != rb and not would_violate(ra, rb):
            dsu.union(ca, cb)
    merged: dict[int, list[int]] = {}
    for ci in range(len(cluster_list)):
        merged.setdefault(dsu.find(ci), []).extend(cluster_list[ci])
    return list(merged.values())


def _apply_manual_pins(conn, now) -> set:
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
    name_to_pid = {}
    for p in conn.execute("SELECT id, name FROM persons WHERE name IS NOT NULL AND name != ''"):
        name_to_pid.setdefault(p["name"], p["id"])
    affected: set = set()
    for f in pins:
        name, cur_pid = f["manual_person"], f["person_id"]
        pid = name_to_pid.get(name)
        if pid is None:
            cur = conn.execute(
                "INSERT INTO persons(name, cover_face_id, face_count, created_at) VALUES(?,?,0,?)",
                (name, f["id"], now),
            )
            pid = cur.lastrowid
            name_to_pid[name] = pid
        if cur_pid != pid:
            conn.execute("UPDATE faces SET person_id=? WHERE id=?", (pid, f["id"]))
            affected.add(pid)
            if cur_pid is not None:
                affected.add(cur_pid)
    return affected


def _refresh_person_stats(conn, pids) -> None:
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


def _finalize(conn, stats, now, progress):
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
    stats.people = conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
    conn.commit()
    if progress is not None:
        progress.update(stats.faces, 0, f"{stats.people} people")
    return stats


class CoreBuilder:
    """Pass 1: build pure "core" clusters out of HIGH-quality faces only.

    Runs the three stages from the module docstring over whatever matrix it is
    given, and returns clusters as lists of row indices into that matrix. It has
    no idea about the database or about tiers — the caller decides which faces
    are core-eligible, which is what keeps "who may seed a cluster" a single,
    reviewable decision rather than a rule smeared across the algorithm.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def build(self, X, progress=None) -> list[list[int]]:
        import numpy as np
        from sklearn.cluster import AgglomerativeClustering

        cfg = self.cfg
        n = len(X)
        if n < 2:
            return []

        # -- stage 1: tight, pure fragments -------------------------------
        # faces_core_link_sim, not faces_link_sim: cores are meant to be
        # unambiguous, and stages 2-3 below are what reassemble a person whose
        # fragments this strictness split apart.
        dsu = _mutual_knn(X, cfg.faces_knn_k, cfg.faces_core_link_sim, progress=progress)
        frag: dict[int, list[int]] = {}
        for i in range(n):
            frag.setdefault(dsu.find(i), []).append(i)
        # Keep only multi-face fragments; singletons are ambiguous → unassigned.
        frags = [idxs for idxs in frag.values() if len(idxs) >= 2]
        if not frags:
            return []
        self.fragments = len(frags)

        # -- stage 2: average-linkage merge of fragment centroids ----------
        cent = np.array([X[idxs].mean(0) for idxs in frags], dtype="float32")
        cent /= np.linalg.norm(cent, axis=1, keepdims=True) + 1e-9
        if len(frags) == 1:
            merge = [0]
        else:
            # AVERAGE linkage, not complete. Complete linkage merges two groups
            # only when *every* cross pair is within threshold, so a high-variance
            # identity (e.g. the same person as a baby and as an adult) never
            # coalesces — its own spread blocks the merge, and the most-photographed
            # person shattered into ~30 fragments. Average linkage keys on the MEAN
            # cross-pair distance, tolerating that spread. It can't chain here
            # because stage 1 already stripped the bridge faces and the margin is
            # huge: measured on this archive, two *different* people's centroids are
            # <=~0.30 cosine while one person's sub-clusters are ~0.75-0.97, so
            # `faces_merge_sim` sits in a wide empty gap — distinct identities stay
            # apart, split selves rejoin.
            #
            # Precomputed float32 cosine-distance matrix (vs metric="cosine") keeps
            # it float32 not sklearn's float64 — half the memory — which matters
            # because mutual-kNN yields several thousand fragments (this O(F²)
            # matrix is the peak).
            dist = (1.0 - cent @ cent.T).astype("float32")
            np.clip(dist, 0.0, 2.0, out=dist)
            merge = AgglomerativeClustering(
                n_clusters=None,
                distance_threshold=1.0 - cfg.faces_merge_sim,
                metric="precomputed",
                linkage="average",
            ).fit_predict(dist)

        clusters: dict[int, list[int]] = {}
        for fi, m in enumerate(merge):
            clusters.setdefault(int(m), []).extend(frags[fi])
        cluster_list = list(clusters.values())

        # -- stage 3: centroid merge --------------------------------------
        # Stage 2's average linkage keys on the MEAN cross-pair similarity, which a
        # loose (high-variance) cluster drags down: a tight cluster and a spread-out
        # cluster of the *same* person can point the same direction (centroids ~0.62
        # cosine) yet have a mean cross-pair of only ~0.36 — under faces_merge_sim,
        # so they never join, and one identity shows up as two people. This pass
        # fixes that by re-merging whole clusters on their *centroid direction*
        # (which divides out the spread): average-link the cluster centroids and
        # merge while centroid cosine >= faces_centroid_merge_sim. Safe because that
        # threshold (~0.55) sits far above where different people's centroids top
        # out (~0.30, measured) — validated to reunite split selves with zero
        # collisions among the named people. Runs on all stage-2 clusters (the
        # min_faces cut is applied after), so a small stray fragment can still
        # rejoin the person it belongs to.
        if len(cluster_list) > 1:
            ccent = np.array([X[idxs].mean(0) for idxs in cluster_list], dtype="float32")
            ccent /= np.linalg.norm(ccent, axis=1, keepdims=True) + 1e-9
            cdist = (1.0 - ccent @ ccent.T).astype("float32")
            np.clip(cdist, 0.0, 2.0, out=cdist)
            clab = AgglomerativeClustering(
                n_clusters=None,
                distance_threshold=1.0 - cfg.faces_centroid_merge_sim,
                metric="precomputed",
                linkage="average",
            ).fit_predict(cdist)
            merged: dict[int, list[int]] = {}
            for ci, cl in enumerate(clab):
                merged.setdefault(int(cl), []).extend(cluster_list[ci])
            cluster_list = list(merged.values())
        return cluster_list


class BorderAssigner:
    """Pass 2: attach BORDERLINE faces to an existing core, or call them noise.

    The asymmetry with pass 1 is the whole point. A borderline face may *join* a
    core; it may not create one and it may not merge two. A bridge face — the
    thing that fuses two identities under any single-linkage scheme — can only do
    damage by causing a merge, and here it has no mechanism to cause one. The
    worst it can do is attach itself to one person's cluster, which is a single
    misplaced face rather than two people collapsed together.

    Similarity is measured against a core's ``faces_border_votes`` most-similar
    *members*, not its centroid: a spread-out core's mean vector understates how
    close it is to its own members (the same effect that made stage 3 necessary),
    which would strand borderline faces around exactly the people who are
    photographed most.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def assign(
        self, X, cores: list[list[int]], border_idx, cannot: set | None = None, face_ids=None
    ) -> dict[int, int]:
        """Map ``{row index of X -> core position}`` for those that attach."""
        import numpy as np

        cfg = self.cfg
        if not cores or len(border_idx) == 0:
            return {}

        # One flat index over every core member, with a parallel owner array, so
        # a single search answers "which cores is this face near, and how near".
        members = np.concatenate([np.asarray(c, dtype=np.int64) for c in cores])
        owner = np.concatenate([np.full(len(c), ci, dtype=np.int32) for ci, c in enumerate(cores)])
        M = np.ascontiguousarray(X[members])
        Q = np.ascontiguousarray(X[np.asarray(border_idx, dtype=np.int64)])

        votes = max(1, int(cfg.faces_border_votes))
        # Retrieve a few times more neighbours than we vote on, so that several
        # distinct cores are usually represented among a face's hits and the best
        # one can actually win rather than being crowded out by one big core.
        want = int(min(len(M), max(votes * 5, 20)))
        faiss = _faiss()
        if faiss is not None:
            index = faiss.IndexFlatIP(M.shape[1])
            index.add(M)
            sims, idx = index.search(Q, want)
        else:  # pragma: no cover - exercised only without faiss
            full = Q @ M.T
            idx = np.argsort(-full, axis=1)[:, :want]
            sims = np.take_along_axis(full, idx, axis=1)

        out: dict[int, int] = {}
        for qi, global_i in enumerate(border_idx):
            per_core: dict[int, list[float]] = {}
            # strict: both come from the same FAISS search, so the row of hits
            # and the row of similarities are the same width by construction.
            for hit, sim in zip(idx[qi], sims[qi], strict=True):
                if hit < 0:
                    continue
                per_core.setdefault(int(owner[hit]), []).append(float(sim))
            ranked = sorted(
                (
                    (sum(sorted(v, reverse=True)[:votes]) / min(len(v), votes), ci)
                    for ci, v in per_core.items()
                ),
                reverse=True,
            )
            for score, ci in ranked:
                if score < cfg.faces_border_assign_sim:
                    break  # ranked desc: no later core can pass
                if (
                    cannot
                    and face_ids is not None
                    and self._blocked(face_ids[global_i], cores[ci], face_ids, cannot)
                ):
                    continue  # try the next-best core instead
                out[global_i] = ci
                break
        return out

    @staticmethod
    def _blocked(fid, core, face_ids, cannot) -> bool:
        """True if the user said this face is NOT the same person as a core member.

        A "different" answer is a hard constraint: it must survive a rebuild, and
        it outranks any similarity the embeddings happen to show.
        """
        return any(frozenset((fid, face_ids[m])) in cannot for m in core)


def _cannot_pairs(conn) -> set:
    return {
        frozenset((r["face_a"], r["face_b"]))
        for r in conn.execute("SELECT face_a, face_b FROM face_links WHERE kind != 'same'")
    }


def cluster_faces(conn, cfg: Config, progress=None) -> ClusterStats:
    import numpy as np

    db.init_db(conn)
    stats = ClusterStats()
    now = db.now_iso()

    # LOW_QUALITY faces are excluded here and nowhere else needs to know: this is
    # the single gate between the FIQA verdict and clustering. A NULL tier (a row
    # from before the gate existed) is treated as BORDERLINE — clustered, but not
    # trusted to seed a core.
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
    if progress is not None:
        progress.total = stats.faces or 1

    # Remember each named person's face-id set to carry the name across rebuild.
    old_named: list[tuple[str, set]] = []
    for pid, name in conn.execute(
        "SELECT id, name FROM persons WHERE name IS NOT NULL AND name != ''"
    ):
        fids = {r["id"] for r in conn.execute("SELECT id FROM faces WHERE person_id=?", (pid,))}
        if fids:
            old_named.append((name, fids))

    conn.execute("UPDATE faces SET person_id=NULL")
    conn.execute("DELETE FROM persons")

    if stats.faces < cfg.faces_min_faces:
        return _finalize(conn, stats, now, progress)

    face_ids = [r["id"] for r in rows]
    scores = [r["det_score"] or 0.0 for r in rows]
    # Embedding dim is inferred from the stored blob, so a backend switch needs
    # no code change here — only a re-extract.
    dim = len(rows[0]["embedding"]) // 4
    X = np.empty((stats.faces, dim), dtype="float32")
    for i, r in enumerate(rows):
        X[i] = np.frombuffer(r["embedding"], dtype="float32")
    X /= np.linalg.norm(X, axis=1, keepdims=True) + 1e-9

    high_idx = [i for i, r in enumerate(rows) if r["tier"] == "HIGH"]
    border_idx = [i for i, r in enumerate(rows) if r["tier"] != "HIGH"]
    stats.high = len(high_idx)
    stats.borderline = len(border_idx)

    # -- pass 1: cores from HIGH-quality faces only -----------------------
    # If the archive has no tiers yet (a database extracted before the FIQA gate)
    # every face reads as BORDERLINE and there would be nothing to seed cores
    # with. Fall back to seeding from everything: worse noise resistance, but a
    # working result, and the next extract fills the tiers in.
    seeded_from_all = len(high_idx) < cfg.faces_min_faces
    seed_idx = list(range(stats.faces)) if seeded_from_all else high_idx
    builder = CoreBuilder(cfg)
    local_cores = builder.build(X[np.asarray(seed_idx, dtype=np.int64)], progress=progress)
    stats.fragments = getattr(builder, "fragments", 0)
    # Back to global row indices.
    cores = [[seed_idx[i] for i in core] for core in local_cores]
    stats.cores = len(cores)

    if not cores:
        return _finalize(conn, stats, now, progress)

    # -- pass 2: attach borderline faces to a core, or leave them as noise -
    if not seeded_from_all and border_idx:
        assigned = BorderAssigner(cfg).assign(
            X, cores, border_idx, cannot=_cannot_pairs(conn), face_ids=face_ids
        )
        for global_i, ci in assigned.items():
            cores[ci].append(global_i)
        stats.border_assigned = len(assigned)
        if progress is not None:
            progress.update(stats.faces, 0, "attaching borderline faces…")

    cluster_list = cores

    # -- pass 3: fold in the user's "same person?" answers ----------------
    cluster_list = _apply_links(conn, cluster_list, face_ids)

    kept = [idxs for idxs in cluster_list if len(idxs) >= cfg.faces_min_faces]
    fsets = [{face_ids[i] for i in idxs} for idxs in kept]

    # Name carryover, one name → one cluster. A previously-used name is given to
    # the single new cluster it best explains: sort all (name, cluster) overlaps
    # descending and assign greedily, each name used once and each cluster named
    # once. This one-to-one rule is what stops a name from spreading — e.g. an old
    # megacluster named "Noelia" holding many people's faces lands "Noelia" on just
    # the one cluster that inherited the most of them, leaving the rest unnamed.
    #
    # Do NOT also gate on the name covering a *majority* of the new cluster: when a
    # person who was split is reunited (average linkage merging their sub-clusters),
    # the old name sat on only one of those sub-clusters, so it is a minority of the
    # bigger merged cluster — a majority gate would then wrongly DROP the name. The
    # small floor below only rejects incidental 1-2 face overlaps.
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
        pid = cur.lastrowid
        assign.extend((pid, face_ids[i]) for i in idxs)
        stats.people += 1
        stats.clustered += len(idxs)
        if name:
            stats.named += 1

    stats.noise = stats.faces - stats.clustered
    conn.executemany("UPDATE faces SET person_id=? WHERE id=?", assign)
    return _finalize(conn, stats, now, progress)
