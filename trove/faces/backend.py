"""Local face detector + quality-aware embedder (SCRFD + AdaFace).

Self-contained and offline after a one-time model-weights download. Detection is
InsightFace **SCRFD** (``det_10g``): bounding box + five landmarks + a confidence
per face, fetched once from the buffalo_l pack into ``cache/models/insightface/``.
Each face is aligned to the standard 112x112 ArcFace template (the 5-point
similarity transform, ``insightface.utils.face_align.norm_crop``) and embedded by
**AdaFace ir101 / WebFace12M** into a 512-d vector — a self-exported ONNX in
``cache/models/adaface/`` (see tools/build/adaface_export.py).

SCRFD replaced the old YuNet detector (far fewer sky/wall false positives, better
small-face recall and landmarks) and is kept. The *embedder* moved back to AdaFace
from buffalo_l's ArcFace for one reason: AdaFace's feature norm is a usable
face-image-quality signal. AdaFace's whole premise (CVPR 2022) is that ‖z‖ of a
margin-softmax model tracks image quality — it is what the model itself uses to
set its adaptive margin. That gives the quality gate in faces/fiqa.py a real
per-face score for free, with no second model and no extra forward pass, which is
what keeps blurry / extreme-profile / false-positive faces out of clustering.

So ``_embed`` returns **both** halves: the L2-normalized vector (cosine similarity
is then a plain dot product — see faces/cluster.py) and the raw pre-normalization
norm, which is the quality signal. Everything runs on onnxruntime's CPU provider —
no torch at runtime, nothing leaves the machine.

Graceful optional dependency, exactly like exiftool/Pillow elsewhere: if OpenCV,
onnxruntime or insightface are unavailable, ``available()`` is False and the faces
feature reports that instead of crashing.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import model_manifest
from ..errors import ModelUnavailableError

if TYPE_CHECKING:
    # fiqa imports Log from here, so the dependency can only go one way at
    # runtime; the assessor type is needed for the constructor's signature.
    from .fiqa import QualityAssessor

logger = logging.getLogger(__name__)

try:
    import cv2
    import numpy as np
except Exception:  # pragma: no cover - optional dep
    # Broad on purpose: a half-installed OpenCV/numpy can fail in more ways than
    # ImportError (e.g. a missing shared library surfaces as OSError). available()
    # below is the single source of truth for "faces works here"; log at DEBUG
    # only, since running without this optional dependency is supported.
    logger.debug("cv2/numpy import failed; face backend will report unavailable", exc_info=True)
    # Runtime sentinels, not a second type for either name: every use below is
    # reached only past `available()`, so the checker keeps grading this file
    # against the real cv2 and numpy rather than against `Any`.
    cv2 = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]

# onnxruntime and insightface ship no type information, so a session or a
# detector is `Any` to the checker whatever we call it. Naming them keeps the
# signatures below saying what they hold instead of shrugging -- the aliases
# are documentation, not enforcement.
Session = Any
Detector = Any

# What a caller passes to watch a one-time model download.
Log = Callable[[str], None]

# buffalo_l bundles five ONNX models in one zip; we only need the DETECTOR now
# (embedding is AdaFace, below). Fetched once from the insightface release,
# extracted into cache/models/insightface/. Sizes are checked after extraction to
# catch a truncated download.
INSIGHTFACE_SUBDIR = "insightface"
DET_MODEL = "det_10g.onnx"
BUFFALO_URL = "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"
_MIN_SIZES = {DET_MODEL: 10_000_000}

# AdaFace embedder: a self-exported ONNX (from the WebFace12M checkpoint),
# consumed by onnxruntime. Unlike buffalo_l it has no upstream URL of its own, so
# it is resolved through the manifest instead (model_manifest.py): a packaged
# build's bundle, this checkout's packaging/models/staged, the download cache, or
# the manifest's release asset. Exactly like the DINOv2 pet model.
ADAFACE_MODEL_NAME = "adaface"

# Identity of the vector space the `faces` table holds. Stored per archive in
# app_state; when it stops matching, every embedding on disk was produced by a
# different model and is unusable, so the archive re-extracts itself (see
# faces/migrate_adaface.py). BUMP THIS whenever the embedder or its
# preprocessing changes — that is what makes the re-extract automatic.
EMBEDDER_VERSION = "adaface-ir101-w12m-v1"


def available() -> bool:
    """True if OpenCV, onnxruntime and insightface are importable."""
    if cv2 is None or np is None:
        return False
    try:
        # Importing is the probe; the names are unused on purpose.
        import insightface  # noqa: F401
        import onnxruntime  # noqa: F401

        return True
    except Exception:  # pragma: no cover - optional dep
        # Broad on purpose: a half-installed insightface/onnxruntime can raise far
        # more than ImportError (missing shared library -> OSError, mismatched
        # onnxruntime build -> RuntimeError). Narrowing this would turn graceful
        # degradation into a crash on a real user's machine. DEBUG only: "faces
        # not installed" is a supported configuration, not a problem.
        logger.debug("insightface/onnxruntime unavailable", exc_info=True)
        return False


def _models_dir(cache_dir: str) -> Path:
    return Path(cache_dir) / "models" / INSIGHTFACE_SUBDIR


def adaface_model_path(cache_dir: str) -> Path:
    """Where the AdaFace ONNX is, or where a download would put it.

    Sibling of the insightface dir, not inside it — it does not come from the
    buffalo_l pack. The search order lives in model_manifest.present(); mirrors
    pets.backend.dinov2_model_path.
    """
    return model_manifest.path(ADAFACE_MODEL_NAME, cache_dir)


def adaface_ready(cache_dir: str) -> bool:
    return model_manifest.present(ADAFACE_MODEL_NAME, cache_dir) is not None


def preflight(cache_dir: str) -> None:
    """Raise if a weight this backend needs can never be obtained here.

    Network-free and cheap, so the detect stage can ask *before* it spends
    ~275 MB fetching buffalo_l — the old order downloaded the detector first and
    only then discovered the embedder was unobtainable, which is the worst of
    both outcomes. Says nothing about whether a download will succeed; that is
    the constructor's problem, and its failure is already reported per detector.
    """
    reason = model_manifest.missing_reason(
        ADAFACE_MODEL_NAME, cache_dir, feature="people detection"
    )
    if reason:
        raise ModelUnavailableError(reason)


def models_ready(cache_dir: str) -> bool:
    """True when BOTH the SCRFD detector and the AdaFace embedder are present.

    The detector self-heals (ensure_models downloads it); AdaFace does not, so a
    missing AdaFace ONNX is a hard, actionable error rather than a silent
    fallback — falling back to a different embedder would put two incompatible
    vector spaces in one `faces` table.
    """
    d = _models_dir(cache_dir)
    return adaface_ready(cache_dir) and all(
        (d / name).is_file() and (d / name).stat().st_size >= min_size
        for name, min_size in _MIN_SIZES.items()
    )


def _detector_ready(cache_dir: str) -> bool:
    d = _models_dir(cache_dir)
    return all(
        (d / name).is_file() and (d / name).stat().st_size >= min_size
        for name, min_size in _MIN_SIZES.items()
    )


def ensure_models(cache_dir: str, log: Log | None = None) -> Path:
    """Download the buffalo_l pack once and extract the detector ONNX we use.

    Atomic per file (temp + rename) so an interrupted extract never leaves a
    half-written model that would fail silently later. Returns the models dir.
    Only the SCRFD detector is fetchable here; the AdaFace embedder has no
    upstream URL and is checked separately by the backend constructor.
    """
    d = _models_dir(cache_dir)
    d.mkdir(parents=True, exist_ok=True)
    if _detector_ready(cache_dir):
        return d
    if log:
        log("downloading face models (buffalo_l) …")
    fd, tmp_zip = tempfile.mkstemp(dir=str(d), suffix=".zip")
    os.close(fd)
    try:
        # Reported like every other weight this app fetches: the pack is ~275 MB,
        # and a single line that never changes for four minutes is what makes a
        # first run look hung. The manifest has no size for it (it is not a
        # manifest entry), so the hook counts megabytes rather than percent --
        # and, for the fetch job, is the one point inside urlretrieve where a
        # cancellation can be noticed at all.
        urllib.request.urlretrieve(
            BUFFALO_URL,
            tmp_zip,
            reporthook=model_manifest.download_progress(log, "face models", 0),
        )
        with zipfile.ZipFile(tmp_zip) as zf:
            for member in zf.namelist():
                base = os.path.basename(member)
                if base not in _MIN_SIZES:
                    continue
                fd2, tmp_part = tempfile.mkstemp(dir=str(d), suffix=".part")
                os.close(fd2)
                try:
                    with zf.open(member) as src, open(tmp_part, "wb") as out:
                        shutil.copyfileobj(src, out)
                    if os.path.getsize(tmp_part) < _MIN_SIZES[base]:
                        raise OSError(
                            f"extracted {base} is too small ({os.path.getsize(tmp_part)} bytes)"
                        )
                    os.replace(tmp_part, d / base)
                finally:
                    if os.path.exists(tmp_part):
                        os.remove(tmp_part)
    finally:
        if os.path.exists(tmp_zip):
            os.remove(tmp_zip)
    if not _detector_ready(cache_dir):
        raise OSError("buffalo_l did not yield det_10g.onnx")
    return d


@dataclass
class Face:
    x: int  # box in ORIGINAL-image pixel coords
    y: int
    w: int
    h: int
    score: float
    embedding: np.ndarray  # float32, L2-normalized, 512-d (AdaFace ir101)
    focus_score: float
    brightness: float
    extreme_fraction: float
    clipped_fraction: float
    quality_score: float
    quality_source: str
    # -- FIQA (faces/fiqa.py) --------------------------------------------
    # fiqa_norm is the RAW pre-normalization AdaFace feature norm; it is stored
    # per face so the archive can be re-tiered later (new thresholds, new
    # calibration) without re-embedding a single image. fiqa_score is that norm
    # mapped to 0..1 against the persisted calibration, and quality_tier is the
    # routing decision: HIGH -> cluster cores, BORDERLINE -> border assignment,
    # LOW_QUALITY -> excluded from clustering and hidden from the GUI.
    fiqa_norm: float = 0.0
    fiqa_score: float = 0.0
    quality_tier: str = "BORDERLINE"


_REJECTION_REASONS = ("score", "size", "focus", "exposure", "clipped", "nonhuman")

# FIQA routing tiers, ordered best-first. Mirrored by faces/fiqa.py and by the
# faces.quality_tier column; kept here so the Face dataclass and the detection
# report can name them without importing the fiqa module (which imports config).
_QUALITY_TIERS = ("HIGH", "BORDERLINE", "LOW_QUALITY")


@dataclass
class DetectionReport:
    """Accepted faces plus auditable counts for each quality-gate decision.

    ``tiers`` counts the FIQA routing outcome over the accepted faces. LOW_QUALITY
    is a *tier*, not a rejection: those faces are still returned and stored (so the
    decision stays reviewable and re-tierable), they are merely kept out of
    clustering and out of the GUI.
    """

    faces: list[Face] = field(default_factory=list)
    candidates: int = 0
    rejected: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_REJECTION_REASONS, 0))
    tiers: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_QUALITY_TIERS, 0))


@dataclass(frozen=True)
class FaceQuality:
    focus_score: float
    brightness: float
    extreme_fraction: float
    quality_score: float


def measure_face_quality(aligned_bgr: np.ndarray, min_focus: float) -> FaceQuality:
    """Measure an aligned 112px crop using deterministic local image metrics.

    Advisory only now (SCRFD confidence is the primary filter): these values are
    stored per face for display and calibration but no longer gate the live path.
    Laplacian variance catches defocus/motion blur; ``extreme_fraction`` catches
    crops dominated by crushed blacks or blown highlights.
    """
    gray = cv2.cvtColor(aligned_bgr, cv2.COLOR_BGR2GRAY)
    focus = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    brightness = float(gray.mean())
    extreme = float(np.mean((gray <= 8) | (gray >= 247)))
    focus_norm = focus / (focus + max(float(min_focus), 1.0))
    exposure_norm = max(0.0, 1.0 - extreme)
    return FaceQuality(
        focus_score=focus,
        brightness=brightness,
        extreme_fraction=extreme,
        quality_score=max(0.0, min(1.0, focus_norm * exposure_norm)),
    )


class FaceBackend:
    """SCRFD detector + AdaFace embedder pair, holding the loaded ONNX models.

    Not thread-safe (the underlying onnxruntime sessions aren't shared safely
    across face jobs); the app runs at most one detection job at a time, so a
    single instance per job is enough.

    ``assessor`` is an optional faces/fiqa.py QualityAssessor. When given, every
    accepted face is scored and tiered here, so the caller only ever sees faces
    that already carry their routing decision. When absent (calibration probes,
    tests, the standalone tools) faces come back tiered BORDERLINE, which is the
    safe default: clustered, but never used to seed a core.
    """

    # Internal detector floor: keep it below ``min_score`` so weak detections are
    # still returned and counted as score-rejections in the report (auditable),
    # rather than silently dropped inside SCRFD.
    _DET_THRESH = 0.30

    def __init__(
        self,
        cache_dir: str,
        *,
        min_score: float = 0.50,
        min_px: int = 50,
        max_side: int = 960,
        det_size: int = 640,
        max_clipped_fraction: float = 0.18,
        min_focus: float = 35.0,
        max_extreme_fraction: float = 0.80,
        quality_version: str = "opencv-laplacian-v1",
        assessor: QualityAssessor | None = None,
        log: Log | None = None,
        **_ignored: Any,
    ) -> None:
        if not available():
            raise ModelUnavailableError(
                "face backend unavailable; install the 'faces' extra "
                "(insightface + onnxruntime) and a modern opencv-python."
            )
        from insightface.model_zoo import get_model

        # The embedder first, deliberately: it is the weight that can be
        # unobtainable (see preflight), and fetching 275 MB of detector before
        # finding that out is bandwidth nobody gets back.
        ada_path = model_manifest.ensure(ADAFACE_MODEL_NAME, cache_dir, log=log)
        d = ensure_models(cache_dir, log=log)
        self.min_score = min_score
        self.min_px = min_px
        self.max_side = max_side
        self.det_size = (int(det_size), int(det_size))
        self.max_clipped_fraction = max_clipped_fraction
        self.min_focus = min_focus
        self.max_extreme_fraction = max_extreme_fraction
        self.quality_version = quality_version
        self.assessor = assessor
        providers = ["CPUExecutionProvider"]
        self._det = get_model(str(d / DET_MODEL), providers=providers)
        self._det.prepare(
            ctx_id=-1, input_size=self.det_size, det_thresh=min(self._DET_THRESH, min_score)
        )
        self._ada = self._load_adaface(ada_path)

    def _load_adaface(self, model_path: Path) -> Session:
        """Open the self-exported AdaFace ONNX session.

        Deliberately fails loudly rather than falling back to another embedder:
        a `faces` table holding vectors from two different models is silently
        broken (cosine between them is meaningless), and the damage only shows up
        much later as nonsense clusters. ``model_manifest.ensure`` has already
        made that failure a clear one — a resolved, hash-verified path or a
        ModelUnavailableError naming the export tool.
        """
        import onnxruntime as ort

        so = ort.SessionOptions()
        so.intra_op_num_threads = os.cpu_count() or 4
        return ort.InferenceSession(str(model_path), so, providers=["CPUExecutionProvider"])

    def _embed(self, aligned_bgr: np.ndarray) -> tuple[np.ndarray, float] | None:
        """112x112 aligned BGR crop -> (unit vector, raw norm), or None.

        The raw pre-normalization norm is returned alongside the unit vector
        because it is AdaFace's image-quality signal (see the module docstring):
        clustering wants the direction, the quality gate wants the magnitude.

        The exported graph has TWO outputs — ``embedding`` (already L2-normalized,
        AdaFace divides by the norm internally) and ``norm``. Take the norm from
        that second output; recomputing it from the first would return 1.0 for
        every face and silently flatten the quality signal to a constant.
        """
        # AdaFace preprocessing: BGR, /255, then (x-0.5)/0.5 -> [-1,1], NCHW.
        x = ((aligned_bgr.astype("float32") / 255.0) - 0.5) / 0.5
        x = x.transpose(2, 0, 1)[None]
        feat_out, norm_out = self._ada.run(None, {"input": x})
        feat = feat_out.reshape(-1).astype("float32")
        norm = float(np.asarray(norm_out).reshape(-1)[0])
        # Re-normalize defensively: the graph already returns a unit vector, but
        # clustering's dot-product-as-cosine assumption must not depend on that.
        n = float(np.linalg.norm(feat))
        return None if n == 0.0 else (feat / n, norm)

    # -- image loading ----------------------------------------------------
    def load_bgr(self, path: str) -> tuple[np.ndarray, float]:
        """(bgr_array, scale) where scale = detected_size / original_size ≤ 1.

        Loads via Pillow (so HEIC and EXIF orientation are handled, matching the
        thumbnailer) and downscales the long side to ``max_side`` for speed.
        Read-only over the original. The fused detect stage decodes once at its
        own resolution and calls ``detect_report`` directly; this is for the
        standalone/calibration paths.
        """
        from PIL import Image, ImageOps

        try:
            import pillow_heif

            pillow_heif.register_heif_opener()
        except Exception:
            # pillow_heif is optional (HEIC support only) and wraps a native
            # libheif binding, so a broken/partial install can fail in more ways
            # than ImportError. Silent on purpose: this runs on every image load,
            # and "HEIC unsupported" is not worth flagging per file.
            pass
        # Two names, because they are two types: `opened` is the ImageFile the
        # decoder hands back (only it has draft()), `im` the plain Image every
        # later step works on.
        with Image.open(path) as opened:
            orig_side = max(opened.size)  # true on-disk size, before draft() shrinks it
            # draft() lets libjpeg downscale in the DCT domain while decoding a
            # JPEG (by 1/2, 1/4, 1/8) — a ~3-4x speedup on load. Must run before
            # any pixel access (i.e. before exif_transpose). No-op for non-JPEG.
            # It silently shrinks im.size as a side effect, so `scale` below is
            # based on `orig_side` captured above to keep boxes mapping back to
            # true original pixels regardless of what draft() did.
            try:
                opened.draft("RGB", (self.max_side, self.max_side))
            except Exception:
                # Best-effort speedup only; fall back to a full decode below
                # rather than aborting the whole file over a draft-only failure.
                pass
            im = ImageOps.exif_transpose(opened).convert("RGB")
            w, h = im.size
            resize_scale = min(1.0, self.max_side / max(w, h)) if max(w, h) else 1.0
            if resize_scale < 1.0:
                im = im.resize((max(1, round(w * resize_scale)), max(1, round(h * resize_scale))))
            arr = np.asarray(im)
        scale = (max(arr.shape[:2]) / orig_side) if orig_side else 1.0
        return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR), scale

    # -- detection + embedding -------------------------------------------
    @staticmethod
    def _clipped_fraction(
        x: float, y: float, w: float, h: float, image_w: int, image_h: int
    ) -> float:
        area = max(0.0, w) * max(0.0, h)
        if area == 0.0:
            return 1.0
        inside_w = max(0.0, min(x + w, image_w) - max(x, 0.0))
        inside_h = max(0.0, min(y + h, image_h) - max(y, 0.0))
        return max(0.0, min(1.0, 1.0 - (inside_w * inside_h / area)))

    def probe_faces(self, img_bgr: np.ndarray) -> list[float]:
        """Face confidences only — no alignment, no embedding.

        For comparing the same photo at several rotations, where only "which way
        up produces faces" matters. ArcFace is the expensive half of a
        detect_report call, so skipping it keeps an orientation probe cheap.
        """
        bboxes, _kpss = self._det.detect(img_bgr, input_size=self.det_size)
        if bboxes is None or len(bboxes) == 0:
            return []
        return [float(b[4]) for b in bboxes]

    def detect_report(
        self, img_bgr: np.ndarray, scale: float = 1.0, *, apply_quality_gate: bool = True
    ) -> DetectionReport:
        """Detect + embed faces in a preloaded BGR image.

        ``scale`` maps ``img_bgr`` coords back to the true original: boxes are
        stored in original pixels (``* 1/scale``) so crops stay sharp even though
        detection ran on a downscaled copy. SCRFD internally resizes to
        ``det_size`` and returns coords in ``img_bgr`` space.
        """
        from insightface.utils import face_align

        report = DetectionReport()
        h, w = img_bgr.shape[:2]
        bboxes, kpss = self._det.detect(img_bgr, input_size=self.det_size)
        if bboxes is None or len(bboxes) == 0:
            return report
        inv = 1.0 / scale if scale else 1.0
        # strict: the detector returns one keypoint set per box, so a length
        # mismatch would silently pair keypoints with the wrong face.
        for box, kps in zip(bboxes, kpss, strict=True):
            report.candidates += 1
            x1, y1, x2, y2 = (float(v) for v in box[:4])
            score = float(box[4])
            bw, bh = x2 - x1, y2 - y1
            if score < self.min_score:
                report.rejected["score"] += 1
                continue
            # min_px is judged in ORIGINAL pixels (what the user actually sees).
            if min(bw, bh) * inv < self.min_px:
                report.rejected["size"] += 1
                continue
            clipped = self._clipped_fraction(x1, y1, bw, bh, w, h)
            if apply_quality_gate and clipped > self.max_clipped_fraction:
                report.rejected["clipped"] += 1
                continue
            aligned = face_align.norm_crop(img_bgr, kps, image_size=112)
            quality = measure_face_quality(aligned, self.min_focus)
            embedded = self._embed(aligned)
            if embedded is None:
                continue
            feat, norm = embedded
            face = Face(
                x=max(0, round(x1 * inv)),
                y=max(0, round(y1 * inv)),
                w=round(bw * inv),
                h=round(bh * inv),
                score=score,
                embedding=feat,
                focus_score=quality.focus_score,
                brightness=quality.brightness,
                extreme_fraction=quality.extreme_fraction,
                clipped_fraction=clipped,
                quality_score=quality.quality_score,
                quality_source=self.quality_version,
                fiqa_norm=norm,
            )
            # Phase 1 routing. The base filters above (score/size/clipping) run
            # BEFORE embedding so cheap rejections stay cheap; the FIQA score is
            # a by-product of the embedding itself, so its gate necessarily runs
            # here. Same effect on cluster purity, less compute than a separate
            # quality model would cost.
            if self.assessor is not None:
                face.fiqa_score = self.assessor.score(face)
                face.quality_tier = self.assessor.tier_of(face, face.fiqa_score)
            report.tiers[face.quality_tier] = report.tiers.get(face.quality_tier, 0) + 1
            report.faces.append(face)
        return report

    def detect(self, img_bgr: np.ndarray, scale: float = 1.0) -> list[Face]:
        """Compatibility wrapper returning only accepted faces."""
        return self.detect_report(img_bgr, scale).faces

    def process_path(self, path: str) -> list[Face]:
        """Compatibility wrapper returning only accepted faces."""
        return self.process_path_report(path).faces

    def process_path_report(self, path: str, *, apply_quality_gate: bool = True) -> DetectionReport:
        img, scale = self.load_bgr(path)
        return self.detect_report(img, scale, apply_quality_gate=apply_quality_gate)
