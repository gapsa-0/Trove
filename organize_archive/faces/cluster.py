"""Cluster face embeddings into people — two-stage, chaining-resistant.

Plain DBSCAN chains distinct identities together through low-quality "bridge"
faces (blurry / profile / occluded), whose embeddings sit in a mushy middle of
the space and are weakly similar to everyone. On this archive a single DBSCAN
cluster swallowed ~55% of all faces. The fix (the mainstream approach for photo
face grouping) is two stages:

  1. **Over-cluster tightly.** Link two faces only when their cosine similarity
     is very high (``faces_link_sim``), via a blocked matrix product + union-find.
     This yields many small, *pure* fragments and leaves unique/ambiguous faces
     as singletons — which we drop rather than let them bridge identities.
  2. **Merge fragment centroids** with complete-linkage agglomerative clustering
     at a looser threshold (``faces_merge_sim``). Complete linkage joins two
     groups only when *every* cross pair is close, so it cannot chain the way
     DBSCAN's single-linkage-style density connectivity does.

Idempotent rebuild; user-assigned names carried over by face-id overlap (like
geo/clusters.py). Only non-hidden files are clustered, so byte-identical takeout
duplicates don't inflate density.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Config
from ..db import database as db


@dataclass
class ClusterStats:
    people: int = 0        # clusters kept as a person
    faces: int = 0         # faces considered (non-hidden)
    clustered: int = 0     # faces assigned to a person
    noise: int = 0         # faces left unassigned (singletons / sub-min clusters)
    named: int = 0         # people that inherited a name from a prior run
    fragments: int = 0     # stage-1 multi-face fragments fed to the merge


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


def _overcluster(X, link_sim: float, block: int = 1024, progress=None):
    """Union all face pairs with cosine similarity >= link_sim.

    Similarities come from a blocked GEMM (X is unit-normalized, so X·Xᵀ is
    cosine) to stay fast and bounded in memory for tens of thousands of faces.
    Only the upper triangle is unioned so each undirected pair is handled once.
    """
    import numpy as np
    n = len(X)
    dsu = _DSU(n)
    for i0 in range(0, n, block):
        i1 = min(n, i0 + block)
        sims = X[i0:i1] @ X.T                       # (b, n)
        rr, cc = np.nonzero(sims >= link_sim)
        for r, c in zip(rr.tolist(), cc.tolist()):
            gi = i0 + r
            if c > gi:                              # upper triangle only
                dsu.union(gi, c)
        if progress is not None:
            progress.update(i1, 0, "grouping faces…")
    return dsu


def cluster_faces(conn, cfg: Config, progress=None) -> ClusterStats:
    import numpy as np
    from sklearn.cluster import AgglomerativeClustering

    db.init_db(conn)
    stats = ClusterStats()
    now = db.now_iso()

    rows = conn.execute(
        """SELECT fa.id, fa.det_score, fa.embedding
           FROM faces fa JOIN files f ON f.id = fa.file_id
           WHERE f.hidden = 0
           ORDER BY fa.id""").fetchall()
    stats.faces = len(rows)
    if progress is not None:
        progress.total = stats.faces or 1

    # Remember each named person's face-id set to carry the name across rebuild.
    old_named: list[tuple[str, set]] = []
    for pid, name in conn.execute(
            "SELECT id, name FROM persons WHERE name IS NOT NULL AND name != ''"):
        fids = {r["id"] for r in conn.execute(
            "SELECT id FROM faces WHERE person_id=?", (pid,))}
        if fids:
            old_named.append((name, fids))

    conn.execute("UPDATE faces SET person_id=NULL")
    conn.execute("DELETE FROM persons")

    if stats.faces < cfg.faces_min_faces:
        conn.commit()
        return stats

    face_ids = [r["id"] for r in rows]
    scores = [r["det_score"] or 0.0 for r in rows]
    # Embedding dim is inferred from the stored blob (512-d AdaFace / 128-d
    # SFace), so a backend switch needs no code change here — only a re-extract.
    dim = len(rows[0]["embedding"]) // 4
    X = np.empty((stats.faces, dim), dtype="float32")
    for i, r in enumerate(rows):
        X[i] = np.frombuffer(r["embedding"], dtype="float32")
    X /= np.linalg.norm(X, axis=1, keepdims=True) + 1e-9

    # -- stage 1: tight, pure fragments -----------------------------------
    dsu = _overcluster(X, cfg.faces_link_sim, progress=progress)
    frag: dict[int, list[int]] = {}
    for i in range(stats.faces):
        frag.setdefault(dsu.find(i), []).append(i)
    # Keep only multi-face fragments; singletons are ambiguous → left unassigned.
    frags = [idxs for idxs in frag.values() if len(idxs) >= 2]
    stats.fragments = len(frags)

    if not frags:
        conn.commit()
        return stats

    # -- stage 2: complete-linkage merge of fragment centroids ------------
    cent = np.array([X[idxs].mean(0) for idxs in frags], dtype="float32")
    cent /= np.linalg.norm(cent, axis=1, keepdims=True) + 1e-9
    if len(frags) == 1:
        merge = [0]
    else:
        merge = AgglomerativeClustering(
            n_clusters=None, distance_threshold=1.0 - cfg.faces_merge_sim,
            metric="cosine", linkage="complete").fit_predict(cent)

    clusters: dict[int, list[int]] = {}
    for fi, m in enumerate(merge):
        clusters.setdefault(int(m), []).extend(frags[fi])

    assign: list[tuple[int, int]] = []
    for idxs in clusters.values():
        if len(idxs) < cfg.faces_min_faces:
            continue                              # too small → unassigned
        fset = {face_ids[i] for i in idxs}
        cover = max(idxs, key=lambda i: scores[i])
        name, best = None, 0
        for cand_name, cand_fids in old_named:
            ov = len(fset & cand_fids)
            if ov > best:
                best, name = ov, cand_name
        cur = conn.execute(
            """INSERT INTO persons (name, cover_face_id, face_count, created_at)
               VALUES (?,?,?,?)""", (name, face_ids[cover], len(idxs), now))
        pid = cur.lastrowid
        assign.extend((pid, face_ids[i]) for i in idxs)
        stats.people += 1
        stats.clustered += len(idxs)
        if name:
            stats.named += 1

    stats.noise = stats.faces - stats.clustered
    conn.executemany("UPDATE faces SET person_id=? WHERE id=?", assign)
    conn.commit()
    if progress is not None:
        progress.update(stats.faces, 0, f"{stats.people} people")
    return stats
