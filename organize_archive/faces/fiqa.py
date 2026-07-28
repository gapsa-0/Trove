"""Phase 1 — Face Image Quality Assessment and tier routing.

The clustering problem this solves: low-quality detections (motion blur, extreme
profiles, tiny background faces, and false positives like dolls, posters and
patterned fabric) produce embeddings that sit in a mushy middle of the vector
space, weakly similar to everybody and strongly similar to nobody. Feed them to
any graph-based clustering and they act as *bridges*: one such face links two
different identities, and a handful of them percolate an archive into a single
blob. They also form clusters of their own that are not people at all.

The fix is to never let them into clustering. Every embedded face is scored 0..1
and routed to a tier:

  ``HIGH``        — may seed a cluster core (faces/cluster.py pass 1).
  ``BORDERLINE``  — may only attach to a core someone else formed (pass 2). It
                    can never create a cluster and never merge two, so even if a
                    borderline face IS a bridge, it has nothing to bridge with.
  ``LOW_QUALITY`` — excluded from clustering entirely and hidden throughout the
                    GUI. Never deleted: the row keeps its score and raw norm, so
                    the decision stays auditable and reversible.

**The scorer.** ``AdaFaceNormFIQA`` uses the embedder's own feature norm. AdaFace
(CVPR 2022) is built on the observation that ‖z‖ of a margin-softmax model tracks
image quality — it is the signal the model uses to scale its adaptive margin — so
the quality score costs one extra output tensor, no second model and no second
forward pass. ``CompositeFIQA`` is the fallback for faces embedded by something
that has no such signal: a blend of the deterministic local metrics already
measured on the aligned crop (detector confidence, Laplacian focus, exposure,
clipping, box size).

**Why calibration is persisted.** A raw feature norm is a model-specific
magnitude, not a probability, so it only becomes a 0..1 score relative to the
population. Extraction is incremental (`face_scan` gates per file), so computing
that population per batch would make a face's tier depend on *when it was
scanned* — two faces of identical quality could land in different tiers, and
re-running extraction would silently re-tier the archive. Instead the mean/std
are computed once from a sample and stored in `fiqa_calibration`; every face
afterwards is scored against those fixed numbers. Retiering under new thresholds
never needs re-embedding, because `faces.fiqa_norm` keeps the raw value.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Config
from ..db import database as db

HIGH = "HIGH"
BORDERLINE = "BORDERLINE"
LOW_QUALITY = "LOW_QUALITY"
TIERS = (HIGH, BORDERLINE, LOW_QUALITY)


class QualityAssessor:
    """Scores a face 0..1 and routes it to a tier.

    Kept as a tiny explicit interface so a different assessor (CR-FIQA, MagFace,
    SER-FIQ) can be dropped in without touching the backend, the extractors or
    the clusterer — they only ever call ``score`` and ``tier``.
    """

    model = "base"

    def __init__(self, *, high: float, low: float):
        self.high = high
        self.low = low

    def score(self, face) -> float:  # pragma: no cover - interface
        raise NotImplementedError

    def tier(self, score: float) -> str:
        if score >= self.high:
            return HIGH
        if score < self.low:
            return LOW_QUALITY
        return BORDERLINE


@dataclass(frozen=True)
class Calibration:
    """Fixed population statistics of the raw quality signal."""

    model: str
    mean: float
    std: float
    n_faces: int


class AdaFaceNormFIQA(QualityAssessor):
    """Map AdaFace's raw feature norm to 0..1 against a fixed calibration.

    ``clip((‖z‖ - mean) / (std * h), -1, 1)`` rescaled to 0..1 — the AdaFace
    paper's own batch-statistics squash, with the batch statistics replaced by
    persisted archive-wide ones (see the module docstring). ``h`` sets how many
    standard deviations span the usable range: smaller h separates the tiers more
    harshly, larger h makes the score gentler.
    """

    def __init__(self, calibration: Calibration, *, h: float,
                 high: float, low: float):
        super().__init__(high=high, low=low)
        self.calibration = calibration
        self.model = calibration.model
        self.h = h if h > 0 else 0.33

    def score(self, face) -> float:
        return self.score_norm(getattr(face, "fiqa_norm", 0.0) or 0.0)

    def score_norm(self, norm: float) -> float:
        spread = self.calibration.std * self.h
        if spread <= 0:
            # Degenerate calibration (every face the same norm). Refusing to
            # score is better than dividing by ~0 and flinging faces to the
            # extremes: call everything BORDERLINE and let clustering proceed.
            return (self.high + self.low) / 2.0
        z = (float(norm) - self.calibration.mean) / spread
        z = max(-1.0, min(1.0, z))
        return (z + 1.0) / 2.0


class CompositeFIQA(QualityAssessor):
    """Deterministic fallback: blend the local metrics already on the crop.

    Used when no AdaFace norm is available (a face row from an older extract, or
    an embedder without a quality signal). Weaker than the norm — it sees
    sharpness and framing but not recognizability — so it is a floor, not a peer.
    """

    model = "composite-v1"

    def score(self, face) -> float:
        det = float(getattr(face, "score", 0.0) or 0.0)
        # quality_score is already focus x exposure, normalized 0..1.
        local = float(getattr(face, "quality_score", 0.0) or 0.0)
        clipped = float(getattr(face, "clipped_fraction", 0.0) or 0.0)
        side = min(int(getattr(face, "w", 0) or 0), int(getattr(face, "h", 0) or 0))
        # Saturating size term: 50px (the base filter) is poor, 160px+ is plenty.
        size = max(0.0, min(1.0, (side - 50) / 110.0))
        raw = 0.40 * det + 0.35 * local + 0.25 * size
        return max(0.0, min(1.0, raw * (1.0 - clipped)))


class UncalibratedFIQA(QualityAssessor):
    """Pre-calibration placeholder: record the norm, commit to nothing.

    The first faces of a brand-new archive are extracted before any norm
    statistics exist. Rather than invent a calibration from a handful of samples,
    those faces are tagged BORDERLINE — clustered, visible, but not core-eligible
    — and ``bootstrap_calibration`` re-tiers them for real once enough norms have
    accumulated. Tiering is therefore eventually correct without any face being
    wrongly discarded in the meantime.
    """

    model = "uncalibrated"

    def __init__(self):
        super().__init__(high=1.1, low=-0.1)   # nothing reaches either bound

    def score(self, face) -> float:
        return 0.5


# -- calibration storage --------------------------------------------------

def load_calibration(conn, model: str) -> Calibration | None:
    row = conn.execute(
        "SELECT model, mean, std, n_faces FROM fiqa_calibration WHERE model=?",
        (model,)).fetchone()
    if row is None:
        return None
    return Calibration(model=row["model"], mean=row["mean"], std=row["std"],
                       n_faces=row["n_faces"])


def save_calibration(conn, calibration: Calibration) -> None:
    conn.execute(
        """INSERT INTO fiqa_calibration(model, mean, std, n_faces, updated_at)
           VALUES(?,?,?,?,?)
           ON CONFLICT(model) DO UPDATE SET
               mean=excluded.mean, std=excluded.std,
               n_faces=excluded.n_faces, updated_at=excluded.updated_at""",
        (calibration.model, calibration.mean, calibration.std,
         calibration.n_faces, db.now_iso()))


def make_assessor(conn, cfg: Config) -> QualityAssessor:
    """The assessor to score new faces with, given what the DB already knows."""
    calibration = load_calibration(conn, cfg.faces_fiqa_model)
    if calibration is None:
        return UncalibratedFIQA()
    return AdaFaceNormFIQA(calibration, h=cfg.faces_fiqa_h,
                           high=cfg.faces_fiqa_high, low=cfg.faces_fiqa_low)


def compute_calibration(conn, cfg: Config, limit: int | None = None) -> Calibration | None:
    """Mean/std of the stored raw norms, over at most ``limit`` faces.

    Hidden and user-rejected faces are excluded so the statistics describe the
    population that will actually be clustered.
    """
    import statistics
    sql = ("SELECT fa.fiqa_norm n FROM faces fa JOIN files f ON f.id=fa.file_id "
           "WHERE fa.fiqa_norm IS NOT NULL AND f.hidden=0 "
           "AND COALESCE(fa.not_person,0)=0 ORDER BY fa.id")
    params: tuple = ()
    if limit:
        sql += " LIMIT ?"
        params = (limit,)
    norms = [r["n"] for r in conn.execute(sql, params)]
    if len(norms) < 2:
        return None
    return Calibration(model=cfg.faces_fiqa_model, mean=statistics.fmean(norms),
                       std=statistics.pstdev(norms), n_faces=len(norms))


def retier_all(conn, cfg: Config) -> dict[str, int]:
    """Re-score and re-tier every face from its STORED raw norm.

    No re-embedding: that is the whole reason `faces.fiqa_norm` is a column. Used
    by the bootstrap below and by an explicit recalibrate after threshold tuning.
    Faces with no norm (pre-AdaFace rows) fall back to the composite scorer.
    """
    calibration = load_calibration(conn, cfg.faces_fiqa_model)
    if calibration is None:
        return {t: 0 for t in TIERS}
    assessor = AdaFaceNormFIQA(calibration, h=cfg.faces_fiqa_h,
                               high=cfg.faces_fiqa_high, low=cfg.faces_fiqa_low)
    fallback = CompositeFIQA(high=cfg.faces_fiqa_high, low=cfg.faces_fiqa_low)
    counts = {t: 0 for t in TIERS}
    updates = []
    for row in conn.execute(
            """SELECT id, fiqa_norm, det_score AS score, quality_score,
                      clipped_fraction, box_w AS w, box_h AS h FROM faces"""):
        if row["fiqa_norm"] is not None:
            score = assessor.score_norm(row["fiqa_norm"])
            source, tier = assessor.model, assessor.tier(score)
        else:
            score = fallback.score(row)
            source, tier = fallback.model, fallback.tier(score)
        counts[tier] += 1
        updates.append((score, source, tier, row["id"]))
    conn.executemany(
        "UPDATE faces SET fiqa_score=?, fiqa_source=?, quality_tier=? WHERE id=?",
        updates)
    return counts


def bootstrap_calibration(conn, cfg: Config, log=None) -> Calibration | None:
    """Fix the calibration once enough norms exist, then tier the backlog.

    Called after each extraction batch. A no-op once `fiqa_calibration` holds a
    row, so it costs one indexed lookup per batch and never drifts: the archive
    is calibrated exactly once, and only an explicit recalibrate changes it.
    """
    existing = load_calibration(conn, cfg.faces_fiqa_model)
    if existing is not None:
        return existing
    pending = conn.execute(
        "SELECT COUNT(*) FROM faces WHERE fiqa_norm IS NOT NULL").fetchone()[0]
    if pending < max(2, cfg.faces_fiqa_calib_sample):
        return None
    calibration = compute_calibration(conn, cfg, limit=cfg.faces_fiqa_calib_sample)
    if calibration is None:
        return None
    save_calibration(conn, calibration)
    counts = retier_all(conn, cfg)
    if log:
        log(f"FIQA calibrated on {calibration.n_faces} faces "
            f"(mean {calibration.mean:.2f}, std {calibration.std:.2f}); "
            f"tiers: {counts[HIGH]} high / {counts[BORDERLINE]} borderline / "
            f"{counts[LOW_QUALITY]} low-quality")
    return calibration


def recalibrate(conn, cfg: Config, log=None) -> dict[str, int]:
    """Recompute the calibration from ALL stored norms and re-tier.

    The knob to reach for after changing `faces_fiqa_high` / `faces_fiqa_low` /
    `faces_fiqa_h`, or once the archive has grown well past the bootstrap sample.
    Cheap — it never touches an image — but it changes cluster inputs, so a
    recluster should follow.
    """
    calibration = compute_calibration(conn, cfg)
    if calibration is None:
        return {t: 0 for t in TIERS}
    save_calibration(conn, calibration)
    counts = retier_all(conn, cfg)
    conn.commit()
    if log:
        log(f"FIQA recalibrated on {calibration.n_faces} faces; "
            f"tiers: {counts[HIGH]} high / {counts[BORDERLINE]} borderline / "
            f"{counts[LOW_QUALITY]} low-quality")
    return counts
