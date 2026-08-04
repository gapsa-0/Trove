"""Neighbour search over face embeddings, and the mutual-kNN graph built on it.

Split out of ``cluster.py`` because none of it knows what a person is: it takes
an L2-normalized matrix and returns neighbours, or a disjoint-set of fragments.
That is what lets the clustering passes above it be read as an argument about
*thresholds* rather than about linear algebra.

Both searches here are blocked GEMM over NumPy. FAISS is still *used* when it is
installed -- ``IndexFlatIP`` is an exact inner product, which on L2-normalized
vectors is cosine, so it is a speed change and not an accuracy change -- but the
desktop build no longer ships it: 62 MB of wheel, including its own 37 MB copy of
OpenBLAS, to make one clustering pass 1.3-2.3x faster. That pass is a few minutes
against a detect run measured in hours.

The two engines are held to agreeing exactly, by
``tests/integration/test_core_expansion.py``. That is what lets this be an
installer-size decision rather than a clustering decision, and it is why the GEMM
path is written as the reference rather than as a degraded fallback.

Both paths are blocked, and that is load-bearing rather than tidy. The pass-2
search runs borderline faces against core members; the unblocked form of it --
one ``Q @ M.T`` and a full ``argsort`` -- is a single 28 GB allocation at 60k
borderline against 120k cores, where FAISS streams. Removing FAISS without
blocking that would have traded a slower archive for one that cannot finish.
"""

from __future__ import annotations

import logging
from types import ModuleType
from typing import TYPE_CHECKING

from ..progress import Progress

if TYPE_CHECKING:
    # numpy is optional; every function here imports it where it runs.
    import numpy as np

logger = logging.getLogger(__name__)


class DSU:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def faiss_module() -> ModuleType | None:
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


def _knn_faiss(
    faiss: ModuleType, X: np.ndarray, k: int, n: int, progress: Progress | None
) -> tuple[np.ndarray, np.ndarray]:
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


def _knn_gemm(
    X: np.ndarray, k: int, n: int, block: int, progress: Progress | None
) -> tuple[np.ndarray, np.ndarray]:
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


def _topk_faiss(
    faiss: ModuleType, M: np.ndarray, Q: np.ndarray, want: int
) -> tuple[np.ndarray, np.ndarray]:
    """Exact top-``want`` rows of ``M`` for each row of ``Q``, via a flat index."""
    index = faiss.IndexFlatIP(M.shape[1])
    index.add(M)
    # Annotated rather than returned directly: faiss reaches us through
    # ModuleType, so its search result is Any to the checker.
    found: tuple[np.ndarray, np.ndarray] = index.search(Q, want)
    return found


def _topk_gemm(
    M: np.ndarray, Q: np.ndarray, want: int, block: int
) -> tuple[np.ndarray, np.ndarray]:
    """The reference implementation: blocked GEMM, argpartition per row.

    Blocked for the same reason ``_knn_gemm`` is. The unblocked form -- one
    ``Q @ M.T`` and a full ``argsort`` -- is fine on a small archive and
    catastrophic on a large one: 60k borderline faces against 120k core members
    is a single 28 GB float32 allocation, where FAISS streams and never
    materialises the matrix at all. That made the no-FAISS path something that
    existed rather than something that worked.
    """
    import numpy as np

    nq = len(Q)
    sims = np.zeros((nq, want), dtype=np.float32)
    idx = np.full((nq, want), -1, dtype=np.int64)
    for q0 in range(0, nq, block):
        q1 = min(nq, q0 + block)
        full = Q[q0:q1] @ M.T  # (b, len(M))
        # kth must be a valid index; callers clamp want to len(M), and want == len(M)
        # leaves kth == len(M) - 1, which is the last legal one.
        part = np.argpartition(-full, want - 1, axis=1)[:, :want]
        vals = np.take_along_axis(full, part, axis=1)
        order = np.argsort(-vals, axis=1)
        idx[q0:q1] = np.take_along_axis(part, order, axis=1)
        sims[q0:q1] = np.take_along_axis(vals, order, axis=1)
        del full, part, vals, order  # free the (b, len(M)) block promptly
    return sims, idx


def topk_search(
    M: np.ndarray, Q: np.ndarray, want: int, block: int = 1024, use_faiss: bool = True
) -> tuple[np.ndarray, np.ndarray]:
    """Top-``want`` rows of ``M`` for each row of ``Q``, most-similar-first.

    Returns ``(sims, idx)`` in that order, matching ``faiss.Index.search``. Both
    inputs must be L2-normalized, which makes an inner product a cosine.

    The asymmetric sibling of ``knn_search``: that one searches a matrix against
    itself and excludes the self-match, this one searches two different sets and
    keeps every hit. Pass 2 of the clustering (``passes.BorderAssigner``) needs
    the second shape -- borderline faces against core members.
    """
    want = max(1, min(want, len(M)))
    faiss = faiss_module() if use_faiss else None
    if faiss is not None:
        return _topk_faiss(faiss, M, Q, want)
    return _topk_gemm(M, Q, want, block)


def knn_search(
    X: np.ndarray,
    k: int,
    block: int = 1024,
    progress: Progress | None = None,
    use_faiss: bool = True,
) -> tuple[np.ndarray, np.ndarray]:
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


def mutual_knn(
    X: np.ndarray, k: int, floor: float, block: int = 1024, progress: Progress | None = None
) -> DSU:
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
