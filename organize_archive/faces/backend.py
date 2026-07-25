"""Local face detector + embedder, built on InsightFace (buffalo_l).

Self-contained and offline after a one-time model-weights download. Detection is
InsightFace **SCRFD** (``det_10g``): bounding box + five landmarks + a confidence
per face. Each face is aligned to the standard 112x112 ArcFace template (the
5-point similarity transform, ``insightface.utils.face_align.norm_crop``) and
embedded by **ArcFace** (``w600k_r50``) into a 512-d vector. Both ONNX files come
from the buffalo_l pack, fetched once into ``cache/models/insightface/``.

SCRFD replaced the old YuNet detector (far fewer sky/wall false positives, better
small-face recall and landmarks); ArcFace replaced the self-exported AdaFace /
OpenCV SFace embedders. Everything runs on onnxruntime's CPU provider — no torch
at runtime, nothing leaves the machine.

Embeddings are L2-normalized on the way out so cosine similarity is a plain dot
product (which lets clustering use a tree index — see faces/cluster.py).

Graceful optional dependency, exactly like exiftool/Pillow elsewhere: if OpenCV,
onnxruntime or insightface are unavailable, ``available()`` is False and the faces
feature reports that instead of crashing.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

try:
    import cv2
    import numpy as np
except Exception:  # pragma: no cover - optional dep
    cv2 = None
    np = None

# buffalo_l bundles five ONNX models in one zip; we only need the detector and
# the recognition (embedding) model. Fetched once from the insightface release,
# extracted into cache/models/insightface/. Sizes are checked after extraction to
# catch a truncated download.
INSIGHTFACE_SUBDIR = "insightface"
DET_MODEL = "det_10g.onnx"
REC_MODEL = "w600k_r50.onnx"
BUFFALO_URL = (
    "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"
)
_MIN_SIZES = {DET_MODEL: 10_000_000, REC_MODEL: 100_000_000}


def available() -> bool:
    """True if OpenCV, onnxruntime and insightface are importable."""
    if cv2 is None or np is None:
        return False
    try:
        import insightface  # noqa: F401
        import onnxruntime  # noqa: F401
        return True
    except Exception:  # pragma: no cover - optional dep
        return False


def _models_dir(cache_dir: str) -> Path:
    return Path(cache_dir) / "models" / INSIGHTFACE_SUBDIR


def det_model_path(cache_dir: str) -> Path:
    return _models_dir(cache_dir) / DET_MODEL


def rec_model_path(cache_dir: str) -> Path:
    return _models_dir(cache_dir) / REC_MODEL


def models_ready(cache_dir: str) -> bool:
    d = _models_dir(cache_dir)
    return all((d / name).is_file() and (d / name).stat().st_size >= min_size
               for name, min_size in _MIN_SIZES.items())


def ensure_models(cache_dir: str, log=None) -> Path:
    """Download the buffalo_l pack once and extract the two ONNX files we use.

    Atomic per file (temp + rename) so an interrupted extract never leaves a
    half-written model that would fail silently later. Returns the models dir.
    """
    d = _models_dir(cache_dir)
    d.mkdir(parents=True, exist_ok=True)
    if models_ready(cache_dir):
        return d
    if log:
        log("downloading face models (buffalo_l) …")
    fd, tmp_zip = tempfile.mkstemp(dir=str(d), suffix=".zip")
    os.close(fd)
    try:
        urllib.request.urlretrieve(BUFFALO_URL, tmp_zip)
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
                            f"extracted {base} is too small "
                            f"({os.path.getsize(tmp_part)} bytes)")
                    os.replace(tmp_part, d / base)
                finally:
                    if os.path.exists(tmp_part):
                        os.remove(tmp_part)
    finally:
        if os.path.exists(tmp_zip):
            os.remove(tmp_zip)
    if not models_ready(cache_dir):
        raise OSError("buffalo_l did not yield det_10g.onnx / w600k_r50.onnx")
    return d


@dataclass
class Face:
    x: int          # box in ORIGINAL-image pixel coords
    y: int
    w: int
    h: int
    score: float
    embedding: "np.ndarray"   # float32, L2-normalized, 512-d (ArcFace w600k_r50)
    focus_score: float
    brightness: float
    extreme_fraction: float
    clipped_fraction: float
    quality_score: float
    quality_source: str


_REJECTION_REASONS = (
    "score", "size", "focus", "exposure", "clipped", "nonhuman")


@dataclass
class DetectionReport:
    """Accepted faces plus auditable counts for each quality-gate decision."""

    faces: list[Face] = field(default_factory=list)
    candidates: int = 0
    rejected: dict[str, int] = field(
        default_factory=lambda: {reason: 0 for reason in _REJECTION_REASONS})


@dataclass(frozen=True)
class FaceQuality:
    focus_score: float
    brightness: float
    extreme_fraction: float
    quality_score: float


def measure_face_quality(aligned_bgr, min_focus: float) -> FaceQuality:
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
    """SCRFD detector + ArcFace embedder pair, holding the loaded ONNX models.

    Not thread-safe (the underlying onnxruntime sessions aren't shared safely
    across face jobs); the app runs at most one detection job at a time, so a
    single instance per job is enough.
    """

    # Internal detector floor: keep it below ``min_score`` so weak detections are
    # still returned and counted as score-rejections in the report (auditable),
    # rather than silently dropped inside SCRFD.
    _DET_THRESH = 0.30

    def __init__(self, cache_dir: str, *, min_score: float = 0.50,
                 min_px: int = 36, max_side: int = 960, det_size: int = 640,
                 max_clipped_fraction: float = 0.18,
                 min_focus: float = 35.0, max_extreme_fraction: float = 0.80,
                 quality_version: str = "opencv-laplacian-v1",
                 log=None, **_ignored):
        if not available():
            raise RuntimeError(
                "face backend unavailable; install the 'ai' extra "
                "(insightface + onnxruntime) and a modern opencv-python.")
        from insightface.model_zoo import get_model
        d = ensure_models(cache_dir, log=log)
        self.min_score = min_score
        self.min_px = min_px
        self.max_side = max_side
        self.det_size = (int(det_size), int(det_size))
        self.max_clipped_fraction = max_clipped_fraction
        self.min_focus = min_focus
        self.max_extreme_fraction = max_extreme_fraction
        self.quality_version = quality_version
        providers = ["CPUExecutionProvider"]
        self._det = get_model(str(d / DET_MODEL), providers=providers)
        self._det.prepare(ctx_id=-1, input_size=self.det_size,
                          det_thresh=min(self._DET_THRESH, min_score))
        self._rec = get_model(str(d / REC_MODEL), providers=providers)
        self._rec.prepare(ctx_id=-1)

    def _embed(self, aligned_bgr):
        """112x112 aligned BGR crop -> L2-normalized float32 embedding (512-d)."""
        feat = self._rec.get_feat(aligned_bgr).reshape(-1).astype("float32")
        n = float(np.linalg.norm(feat))
        return None if n == 0.0 else feat / n

    # -- image loading ----------------------------------------------------
    def load_bgr(self, path: str):
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
            pass
        with Image.open(path) as im:
            orig_side = max(im.size)   # true on-disk size, before draft() shrinks it
            # draft() lets libjpeg downscale in the DCT domain while decoding a
            # JPEG (by 1/2, 1/4, 1/8) — a ~3-4x speedup on load. Must run before
            # any pixel access (i.e. before exif_transpose). No-op for non-JPEG.
            # It silently shrinks im.size as a side effect, so `scale` below is
            # based on `orig_side` captured above to keep boxes mapping back to
            # true original pixels regardless of what draft() did.
            try:
                im.draft("RGB", (self.max_side, self.max_side))
            except Exception:
                pass
            im = ImageOps.exif_transpose(im).convert("RGB")
            w, h = im.size
            resize_scale = min(1.0, self.max_side / max(w, h)) if max(w, h) else 1.0
            if resize_scale < 1.0:
                im = im.resize((max(1, round(w * resize_scale)), max(1, round(h * resize_scale))))
            arr = np.asarray(im)
        scale = (max(arr.shape[:2]) / orig_side) if orig_side else 1.0
        return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR), scale

    # -- detection + embedding -------------------------------------------
    @staticmethod
    def _clipped_fraction(x: float, y: float, w: float, h: float,
                          image_w: int, image_h: int) -> float:
        area = max(0.0, w) * max(0.0, h)
        if area == 0.0:
            return 1.0
        inside_w = max(0.0, min(x + w, image_w) - max(x, 0.0))
        inside_h = max(0.0, min(y + h, image_h) - max(y, 0.0))
        return max(0.0, min(1.0, 1.0 - (inside_w * inside_h / area)))

    def probe_faces(self, img_bgr) -> list[float]:
        """Face confidences only — no alignment, no embedding.

        For comparing the same photo at several rotations, where only "which way
        up produces faces" matters. ArcFace is the expensive half of a
        detect_report call, so skipping it keeps an orientation probe cheap.
        """
        bboxes, _kpss = self._det.detect(img_bgr, input_size=self.det_size)
        if bboxes is None or len(bboxes) == 0:
            return []
        return [float(b[4]) for b in bboxes]

    def detect_report(self, img_bgr, scale: float = 1.0,
                      *, apply_quality_gate: bool = True) -> DetectionReport:
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
        for box, kps in zip(bboxes, kpss):
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
            feat = self._embed(aligned)
            if feat is None:
                continue
            report.faces.append(Face(
                x=max(0, round(x1 * inv)), y=max(0, round(y1 * inv)),
                w=round(bw * inv), h=round(bh * inv),
                score=score, embedding=feat,
                focus_score=quality.focus_score,
                brightness=quality.brightness,
                extreme_fraction=quality.extreme_fraction,
                clipped_fraction=clipped,
                quality_score=quality.quality_score,
                quality_source=self.quality_version))
        return report

    def detect(self, img_bgr, scale: float = 1.0) -> list[Face]:
        """Compatibility wrapper returning only accepted faces."""
        return self.detect_report(img_bgr, scale).faces

    def process_path(self, path: str) -> list[Face]:
        """Compatibility wrapper returning only accepted faces."""
        return self.process_path_report(path).faces

    def process_path_report(self, path: str,
                            *, apply_quality_gate: bool = True) -> DetectionReport:
        img, scale = self.load_bgr(path)
        return self.detect_report(
            img, scale, apply_quality_gate=apply_quality_gate)
