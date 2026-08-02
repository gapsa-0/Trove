"""Neighbour search over face embeddings, and the mutual-kNN graph built on it.

Split out of ``cluster.py`` because none of it knows what a person is: it takes
an L2-normalized matrix and returns neighbours, or a disjoint-set of fragments.
That is what lets the clustering passes above it be read as an argument about
*thresholds* rather than about linear algebra.

All search runs through FAISS ``IndexFlatIP`` -- exact inner product, which on
L2-normalized vectors *is* cosine, so it is a speed change and not an accuracy
change. The blocked-GEMM path is kept both as the no-faiss fallback and as the
reference the tests assert FAISS against, so a change of engine can never
quietly become a change of clustering.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class DSU:
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


def faiss_module():
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


def _knn_faiss(faiss, X, k: int, n: int, progress):
    """Exact top-k via a flat inner-product index."""
    import numpy as np

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


def _knn_gemm(X, k: int, n: int, block: int, progress):
    """The reference implementation: blocked GEMM, argpartition per row."""
    import numpy as np

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


def knn_search(X, k: int, block: int = 1024, progress=None, use_faiss: bool = True):
    """Top-``k`` neighbours of every row of ``X``, self-match excluded.

    Returns ``(nbr, nbs)`` — indices and cosine similarities, each ``(n, k)``,
    sorted most-similar-first. ``X`` must be L2-normalized, which makes an inner
    product a cosine. The two backends are interchangeable by construction; see
    the module docstring.
    """
    n = len(X)
    k = max(1, min(k, n - 1))  # can't have more neighbours than faces
    faiss = faiss_module() if use_faiss else None
    if faiss is not None:
        return _knn_faiss(faiss, X, k, n, progress)
    return _knn_gemm(X, k, n, block, progress)


def mutual_knn(X, k: int, floor: float, block: int = 1024, progress=None) -> DSU:
    """Pass-1 over-cluster via a MUTUAL k-NN graph (see ``passes.py``).

    Union two faces only when each is among the other's ``k`` most-similar faces
    *and* their cosine similarity is >= ``floor``. Returns a ``DSU`` whose
    components are the fragments. This replaces the old "union every pair >=
    threshold", which was single-linkage and chained distinct people into one
    giant blob.
    """
    import numpy as np

    n = len(X)
    k = max(1, min(k, n - 1))
    nbr, nbs = knn_search(X, k, block=block, progress=progress)
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

    dsu = DSU(n)
    for kk in mutual.tolist():
        dsu.union(kk // n, kk % n)
    return dsu
