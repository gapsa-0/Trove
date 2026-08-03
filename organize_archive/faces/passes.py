"""The two clustering passes, over embeddings alone -- no database, no ids.

**Pass 1 -- cores** (``CoreBuilder``). Only HIGH-tier faces take part. They are
clustered into "cores" by the three stages below. Because every participant is a
good face, a core is pure by construction.

**Pass 2 -- border assignment** (``BorderAssigner``). BORDERLINE faces are then
matched against the finished cores and attached to one if they are close enough,
otherwise left as noise. A borderline face can *join* a core; it can never
*create* one and never *merge two*. That single restriction is what defuses
bridge faces: the damage a bridge does is always a merge, and a borderline face
is not allowed to cause one.

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
"""

from __future__ import annotations

from ..config import Config
from .knn import mutual_knn, topk_search


def _centroids(X, groups: list[list[int]]):
    """Unit-length mean vector of each group of row indices."""
    import numpy as np

    cent = np.array([X[idxs].mean(0) for idxs in groups], dtype="float32")
    cent /= np.linalg.norm(cent, axis=1, keepdims=True) + 1e-9
    return cent


def _average_link(cent, min_sim: float):
    """Average-linkage agglomerative labels over cosine distance between rows.

    A precomputed float32 cosine-distance matrix (rather than metric="cosine")
    keeps it float32 rather than sklearn's float64 -- half the memory -- which
    matters because mutual-kNN yields several thousand fragments and this O(F^2)
    matrix is the peak of the whole stage.
    """
    import numpy as np
    from sklearn.cluster import AgglomerativeClustering

    dist = (1.0 - cent @ cent.T).astype("float32")
    np.clip(dist, 0.0, 2.0, out=dist)
    return AgglomerativeClustering(
        n_clusters=None,
        distance_threshold=1.0 - min_sim,
        metric="precomputed",
        linkage="average",
    ).fit_predict(dist)


def _regroup(groups: list[list[int]], labels) -> list[list[int]]:
    """Fold ``groups`` together according to a label per group."""
    merged: dict[int, list[int]] = {}
    for gi, lab in enumerate(labels):
        merged.setdefault(int(lab), []).extend(groups[gi])
    return list(merged.values())


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
        self.fragments = 0

    def _fragments(self, X, progress) -> list[list[int]]:
        """Stage 1: tight, pure fragments from the mutual k-NN graph.

        ``faces_core_link_sim``, not ``faces_link_sim``: cores are meant to be
        unambiguous, and stages 2-3 are what reassemble a person whose fragments
        this strictness split apart. Singletons are ambiguous, so they are
        dropped rather than allowed to bridge two identities.
        """
        dsu = mutual_knn(X, self.cfg.faces_knn_k, self.cfg.faces_core_link_sim, progress=progress)
        frag: dict[int, list[int]] = {}
        for i in range(len(X)):
            frag.setdefault(dsu.find(i), []).append(i)
        return [idxs for idxs in frag.values() if len(idxs) >= 2]

    def _merge_fragments(self, X, frags: list[list[int]]) -> list[list[int]]:
        """Stage 2: average-linkage merge of fragment centroids.

        AVERAGE linkage, not complete. Complete linkage merges two groups only
        when *every* cross pair is within threshold, so a high-variance identity
        (e.g. the same person as a baby and as an adult) never coalesces -- its
        own spread blocks the merge, and the most-photographed person shattered
        into ~30 fragments. Average linkage keys on the MEAN cross-pair distance,
        tolerating that spread. It can't chain here because stage 1 already
        stripped the bridge faces and the margin is huge: measured on this
        archive, two *different* people's centroids are <=~0.30 cosine while one
        person's sub-clusters are ~0.75-0.97, so ``faces_merge_sim`` sits in a
        wide empty gap -- distinct identities stay apart, split selves rejoin.
        """
        if len(frags) == 1:
            return [list(frags[0])]
        labels = _average_link(_centroids(X, frags), self.cfg.faces_merge_sim)
        return _regroup(frags, labels)

    def _merge_centroids(self, X, cluster_list: list[list[int]]) -> list[list[int]]:
        """Stage 3: re-merge whole clusters that point the same direction.

        Stage 2's average linkage keys on the MEAN cross-pair similarity, which a
        loose (high-variance) cluster drags down: a tight cluster and a spread-out
        cluster of the *same* person can point the same direction (centroids ~0.62
        cosine) yet have a mean cross-pair of only ~0.36 -- under faces_merge_sim,
        so they never join, and one identity shows up as two people. Comparing
        centroid *direction* divides out the spread. Safe because
        ``faces_centroid_merge_sim`` (~0.55) sits far above where different
        people's centroids top out (~0.30, measured) -- validated to reunite split
        selves with zero collisions among the named people.

        Runs on all stage-2 clusters (the min_faces cut is applied after), so a
        small stray fragment can still rejoin the person it belongs to.
        """
        if len(cluster_list) <= 1:
            return cluster_list
        labels = _average_link(_centroids(X, cluster_list), self.cfg.faces_centroid_merge_sim)
        return _regroup(cluster_list, labels)

    def build(self, X, progress=None) -> list[list[int]]:
        if len(X) < 2:
            return []
        frags = self._fragments(X, progress)
        if not frags:
            return []
        self.fragments = len(frags)
        return self._merge_centroids(X, self._merge_fragments(X, frags))


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

    def _search(self, M, Q, want: int):
        """Top-``want`` core members for each query face, with similarities."""
        return topk_search(M, Q, want)

    def _rank_cores(self, hits, sims, owner, votes: int) -> list[tuple[float, int]]:
        """Score each core this face hit by the mean of its best ``votes`` hits."""
        per_core: dict[int, list[float]] = {}
        # strict: both come from the same search, so the row of hits and the row
        # of similarities are the same width by construction.
        for hit, sim in zip(hits, sims, strict=True):
            if hit < 0:
                continue
            per_core.setdefault(int(owner[hit]), []).append(float(sim))
        return sorted(
            (
                (sum(sorted(v, reverse=True)[:votes]) / min(len(v), votes), ci)
                for ci, v in per_core.items()
            ),
            reverse=True,
        )

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
        sims, idx = self._search(M, Q, want)

        out: dict[int, int] = {}
        for qi, global_i in enumerate(border_idx):
            for score, ci in self._rank_cores(idx[qi], sims[qi], owner, votes):
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
