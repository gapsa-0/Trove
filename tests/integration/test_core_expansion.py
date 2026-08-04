"""Pass-1 / pass-2 core-expansion clustering, and its noise resistance."""

from __future__ import annotations

import pytest

from organize_archive.config import Config
from organize_archive.db import database as db
from organize_archive.faces import cluster as fc
from organize_archive.faces import knn, passes

np = pytest.importorskip("numpy")
# The agglomerative merge stages inside passes.py import scikit-learn at call
# time, so without it these fail from inside the code under test rather than
# skipping. Declared here with numpy because both come from the same extra.
pytest.importorskip("sklearn")


def _unit(v):
    v = np.asarray(v, dtype="float32")
    return v / (np.linalg.norm(v) + 1e-9)


def _identity(seed, n, dim=64, spread=0.06):
    """``n`` tight variations around one random direction — i.e. one person."""
    rng = np.random.default_rng(seed)
    base = _unit(rng.normal(size=dim))
    out = [_unit(base + spread * rng.normal(size=dim)) for _ in range(n)]
    return np.stack(out).astype("float32")


def test_faiss_knn_matches_the_reference_gemm_exactly():
    """Swapping in FAISS must be a speed change, not an accuracy change.

    IndexFlatIP is a *flat* index — it scans everything — so it is exact, and the
    two paths must agree on the neighbours, not merely approximately.
    """
    if knn.faiss_module() is None:
        pytest.skip("faiss is an optional dependency")
    rng = np.random.default_rng(0)
    for n, dim, k in ((200, 64, 5), (500, 128, 8)):
        X = np.stack([_unit(v) for v in rng.normal(size=(n, dim))]).astype("float32")
        fi, fs = knn.knn_search(X, k, use_faiss=True)
        gi, gs = knn.knn_search(X, k, use_faiss=False)
        assert (fi == gi).all()
        assert np.abs(fs - gs).max() < 1e-5


def test_faiss_topk_matches_the_reference_gemm_exactly():
    """The same guarantee for pass 2's search, which is asymmetric.

    ``knn_search`` scans a matrix against itself; ``topk_search`` scans borderline
    faces against core members, and until recently its no-FAISS path was a
    separate, unblocked one-liner in passes.py that nothing exercised. The two
    backends have to agree here for the same reason they do above.
    """
    if knn.faiss_module() is None:
        pytest.skip("faiss is an optional dependency")
    rng = np.random.default_rng(1)
    for n_m, n_q, dim, want in ((300, 120, 64, 5), (64, 500, 128, 12)):
        M = np.stack([_unit(v) for v in rng.normal(size=(n_m, dim))]).astype("float32")
        Q = np.stack([_unit(v) for v in rng.normal(size=(n_q, dim))]).astype("float32")
        fs, fi = knn.topk_search(M, Q, want, use_faiss=True)
        gs, gi = knn.topk_search(M, Q, want, use_faiss=False)
        assert (fi == gi).all()
        assert np.abs(fs - gs).max() < 1e-5


def test_topk_blocking_does_not_change_the_answer():
    """A block boundary must not be visible in the result.

    Similarities are compared to float32 epsilon rather than exactly: BLAS
    dispatches on matrix shape, so a (10, n) product and a (97, n) product take
    different kernels and land ~1e-7 apart. The neighbours themselves are
    identical, which is what the clustering actually consumes.
    """
    rng = np.random.default_rng(2)
    M = np.stack([_unit(v) for v in rng.normal(size=(200, 32))]).astype("float32")
    Q = np.stack([_unit(v) for v in rng.normal(size=(97, 32))]).astype("float32")

    whole = knn.topk_search(M, Q, 7, block=1024, use_faiss=False)
    split = knn.topk_search(M, Q, 7, block=10, use_faiss=False)

    assert (whole[1] == split[1]).all()
    assert np.abs(whole[0] - split[0]).max() < 1e-6


def test_topk_never_asks_for_more_neighbours_than_exist():
    """``want`` above the core count would put argpartition's kth out of range."""
    rng = np.random.default_rng(3)
    M = np.stack([_unit(v) for v in rng.normal(size=(3, 16))]).astype("float32")
    Q = np.stack([_unit(v) for v in rng.normal(size=(5, 16))]).astype("float32")

    sims, idx = knn.topk_search(M, Q, want=50, use_faiss=False)

    assert idx.shape == (5, 3) and sims.shape == (5, 3)
    assert idx.min() >= 0


# 2-3s: the only test here that clusters enough vectors to cost real time.
@pytest.mark.slow
def test_bridge_vectors_cannot_fuse_two_identities():
    """The regression this whole design exists for.

    Two people, plus 'bridge' vectors sitting midway between them — the blurry /
    profile / false-positive detections that used to chain every identity into
    one blob. As BORDERLINE input they may attach to a core but never merge two,
    so the two people must stay two.
    """
    cfg = Config()
    a, b = _identity(1, 25), _identity(2, 25)
    mid = _unit(a.mean(0) + b.mean(0))
    bridges = np.stack(
        [_unit(mid + 0.02 * np.random.default_rng(i).normal(size=64)) for i in range(8)]
    ).astype("float32")

    X = np.concatenate([a, b, bridges]).astype("float32")
    high = list(range(50))  # the two real identities
    border = list(range(50, 58))  # the bridges

    cores = passes.CoreBuilder(cfg).build(X[np.asarray(high)])
    assert len(cores) == 2, f"expected two pure cores, got {len(cores)}"

    cores_global = [[high[i] for i in c] for c in cores]
    assigned = passes.BorderAssigner(cfg).assign(X, cores_global, border)
    for core in cores_global:
        for member in core:
            assert member < 50  # no bridge was ever allowed to seed
    # Whatever the bridges do, the two identities remain two separate clusters.
    assert len(cores_global) == 2
    # And no bridge may end up joining both.
    assert all(isinstance(ci, int) for ci in assigned.values())


def test_a_borderline_face_joins_the_core_it_belongs_to():
    cfg = Config()
    a, b = _identity(3, 20), _identity(4, 20)
    # A borderline face of person A: same direction, just noisier.
    rng = np.random.default_rng(9)
    extra = _unit(a.mean(0) + 0.10 * rng.normal(size=64)).astype("float32")

    X = np.concatenate([a, b, extra[None, :]]).astype("float32")
    high = list(range(40))
    cores = passes.CoreBuilder(cfg).build(X[np.asarray(high)])
    cores_global = [[high[i] for i in c] for c in cores]
    assert len(cores_global) == 2

    assigned = passes.BorderAssigner(cfg).assign(X, cores_global, [40])
    assert 40 in assigned, "a clearly-similar borderline face was left as noise"
    # It joined A's core, not B's.
    joined = cores_global[assigned[40]]
    assert all(i < 20 for i in joined)


def test_an_unrelated_borderline_face_is_left_as_noise():
    cfg = Config()
    a, b = _identity(5, 20), _identity(6, 20)
    stranger = _identity(99, 1)[0]
    X = np.concatenate([a, b, stranger[None, :]]).astype("float32")
    high = list(range(40))
    cores = passes.CoreBuilder(cfg).build(X[np.asarray(high)])
    cores_global = [[high[i] for i in c] for c in cores]

    assigned = passes.BorderAssigner(cfg).assign(X, cores_global, [40])
    assert 40 not in assigned, "an unrelated face was absorbed into a person"


def test_a_cannot_link_blocks_a_border_assignment():
    """A user's 'different' answer outranks whatever the embeddings say."""
    cfg = Config()
    a = _identity(7, 20)
    rng = np.random.default_rng(11)
    extra = _unit(a.mean(0) + 0.08 * rng.normal(size=64)).astype("float32")
    X = np.concatenate([a, extra[None, :]]).astype("float32")
    high = list(range(20))
    cores = passes.CoreBuilder(cfg).build(X[np.asarray(high)])
    cores_global = [[high[i] for i in c] for c in cores]
    assert cores_global

    face_ids = list(range(100, 100 + len(X)))  # arbitrary stable ids
    without = passes.BorderAssigner(cfg).assign(
        X, cores_global, [20], cannot=set(), face_ids=face_ids
    )
    assert 20 in without, "precondition: it attaches when unconstrained"

    cannot = {frozenset((face_ids[20], face_ids[m])) for m in cores_global[0]}
    with_block = passes.BorderAssigner(cfg).assign(
        X, cores_global, [20], cannot=cannot, face_ids=face_ids
    )
    assert 20 not in with_block, "a cannot-link did not block the assignment"


def _catalog(tmp_path, tiers):
    """A database holding one face per tier entry, all on one file."""
    conn = db.connect(tmp_path / "archive.db")
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/r','2026-01-01')")
    conn.execute(
        """INSERT INTO files(id,root_id,rel_path,size,mtime,media_type,
                             first_seen,last_seen,present,hidden)
           VALUES(1,1,'a.jpg',1,0,'image','2026-01-01','2026-01-01',1,0)"""
    )
    for vec, tier in tiers:
        conn.execute(
            """INSERT INTO faces(file_id,box_x,box_y,box_w,box_h,det_score,
                                 quality_tier,embedding,created_at)
               VALUES(1,0,0,60,60,0.9,?,?,'2026-01-01')""",
            (tier, np.asarray(vec, dtype="float32").tobytes()),
        )
    conn.commit()
    return conn


def test_low_quality_faces_never_reach_clustering(tmp_path):
    cfg = Config()
    people = _identity(21, 12)
    rows = [(v, "HIGH") for v in people]
    rows += [(v, "LOW_QUALITY") for v in _identity(22, 6)]
    conn = _catalog(tmp_path, rows)

    stats = fc.cluster_faces(conn, cfg)

    assert stats.low_quality_excluded == 6
    assert stats.faces == 12, "LOW_QUALITY faces were loaded into clustering"
    assigned = conn.execute(
        "SELECT COUNT(*) FROM faces WHERE quality_tier='LOW_QUALITY' AND person_id IS NOT NULL"
    ).fetchone()[0]
    assert assigned == 0, "a LOW_QUALITY face was assigned to a person"
    conn.close()


def test_an_untiered_archive_still_clusters(tmp_path):
    """A database from before the gate has NULL tiers; it must not go empty."""
    cfg = Config()
    conn = _catalog(tmp_path, [(v, None) for v in _identity(31, 12)])
    stats = fc.cluster_faces(conn, cfg)
    assert stats.people >= 1
    assert stats.clustered > 0
    conn.close()
