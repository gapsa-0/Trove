"""Local face detector + embedder.

Self-contained and offline after a one-time model-weights download. Detection is
always OpenCV YuNet (bounding box + five landmarks + confidence per face). The
face is aligned to the standard 112x112 ArcFace template (OpenCV's alignCrop),
then embedded by one of two backends:

  * ``adaface`` (default) — AdaFace ir101 / WebFace12M, a 512-d embedding run via
    onnxruntime. Far stronger on this archive's varied, low-quality, cross-age
    faces than SFace, so identities cluster tightly and split much less. The
    model is a self-exported ONNX in ``cache/models/adaface/`` (torch-free at
    runtime; see tools/adaface_export.py to regenerate it from the checkpoint).
  * ``sface`` — the original OpenCV SFace, 128-d. Lighter, weaker; kept as a
    fallback and because its FaceRecognizerSF also provides ``alignCrop``, which
    both backends reuse for alignment.

Embeddings are L2-normalized on the way out so cosine similarity is a plain dot
product and Euclidean distance on the unit sphere is monotonic in cosine
distance (which lets clustering use a tree index — see faces/cluster.py).

Graceful optional dependency, exactly like exiftool/Pillow elsewhere: if OpenCV
(with the FaceDetectorYN / FaceRecognizerSF APIs) is unavailable, ``available()``
is False and the faces feature reports that instead of crashing.
"""

from __future__ import annotations

import os
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

try:
    import cv2
    import numpy as np
except Exception:  # pragma: no cover - optional dep
    cv2 = None
    np = None

# AdaFace embedder: a self-exported ONNX (from the WebFace12M checkpoint),
# consumed by onnxruntime. Not distributed via a URL like the OpenCV models —
# regenerate it with tools/adaface_export.py if missing.
ADAFACE_SUBDIR = "adaface"
ADAFACE_MODEL = "adaface_ir101_w12m.onnx"


# opencv_zoo distributes these via Git LFS; the media.githubusercontent.com
# "/media/" host serves the real binary (raw.githubusercontent serves only the
# LFS pointer text). Sizes are checked after download to catch a truncated or
# pointer-only fetch.
_LFS = "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models"
_MODELS = {
    "detector": (
        "face_detection_yunet_2023mar.onnx",
        f"{_LFS}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
        200_000,
    ),
    "recognizer": (
        "face_recognition_sface_2021dec.onnx",
        f"{_LFS}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
        30_000_000,
    ),
}


def available() -> bool:
    """True if OpenCV with the DNN face APIs is importable."""
    return cv2 is not None and hasattr(cv2, "FaceDetectorYN") \
        and hasattr(cv2, "FaceRecognizerSF")


def _models_dir(cache_dir: str) -> Path:
    return Path(cache_dir) / "models"


def adaface_model_path(cache_dir: str) -> Path:
    return _models_dir(cache_dir) / ADAFACE_SUBDIR / ADAFACE_MODEL


def models_ready(cache_dir: str) -> bool:
    d = _models_dir(cache_dir)
    for name, _url, min_size in _MODELS.values():
        p = d / name
        if not p.is_file() or p.stat().st_size < min_size:
            return False
    return True


def ensure_models(cache_dir: str, log=None) -> Path:
    """Download any missing model into ``cache_dir/models`` (one-time).

    Atomic per file (temp + rename) so an interrupted download never leaves a
    half-written model that would fail silently later. Returns the models dir.
    """
    d = _models_dir(cache_dir)
    d.mkdir(parents=True, exist_ok=True)
    for name, url, min_size in _MODELS.values():
        p = d / name
        if p.is_file() and p.stat().st_size >= min_size:
            continue
        if log:
            log(f"downloading face model {name} …")
        fd, tmp = tempfile.mkstemp(dir=str(d), suffix=".part")
        os.close(fd)
        try:
            urllib.request.urlretrieve(url, tmp)
            if os.path.getsize(tmp) < min_size:
                raise OSError(
                    f"downloaded {name} is too small "
                    f"({os.path.getsize(tmp)} bytes) — expected the LFS binary")
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    return d


@dataclass
class Face:
    x: int          # box in ORIGINAL-image pixel coords
    y: int
    w: int
    h: int
    score: float
    embedding: "np.ndarray"   # float32, L2-normalized (512-d AdaFace / 128-d SFace)
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

    Laplacian variance catches defocus/motion blur. ``extreme_fraction`` catches
    crops dominated by crushed blacks or blown highlights. The composite is a
    display/reporting score; the individual configured thresholds make the
    accept/reject decision.
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
    """Detector + embedder pair, holding the loaded ONNX models.

    Not thread-safe (the underlying cv2 nets aren't); the app runs at most one
    face job at a time, so a single instance per job is enough.
    """

    def __init__(self, cache_dir: str, *, min_score: float = 0.62,
                 min_px: int = 36, max_side: int = 1280,
                 min_focus: float = 35.0, max_extreme_fraction: float = 0.80,
                 max_clipped_fraction: float = 0.18,
                 quality_version: str = "opencv-laplacian-v1",
                 embed_backend: str = "adaface", log=None):
        if not available():
            raise RuntimeError(
                "OpenCV face APIs unavailable — install the 'media' extra "
                "(a modern opencv-python provides FaceDetectorYN/FaceRecognizerSF).")
        d = ensure_models(cache_dir, log=log)
        self.min_score = min_score
        self.min_px = min_px
        self.max_side = max_side
        self.min_focus = min_focus
        self.max_extreme_fraction = max_extreme_fraction
        self.max_clipped_fraction = max_clipped_fraction
        self.quality_version = quality_version
        self.embed_backend = embed_backend
        # score_threshold here is a coarse pre-filter; we re-check min_score too.
        self._det = cv2.FaceDetectorYN.create(
            str(d / _MODELS["detector"][0]), "", (320, 320),
            min(min_score, 0.5), 0.3, 5000)
        # SFace is always loaded: it embeds in the "sface" backend and provides
        # alignCrop (the 112x112 ArcFace alignment) that AdaFace also consumes.
        self._rec = cv2.FaceRecognizerSF.create(
            str(d / _MODELS["recognizer"][0]), "")
        self._ada = None
        if embed_backend == "adaface":
            self._ada = self._load_adaface(cache_dir)
        elif embed_backend != "sface":
            raise ValueError(f"unknown faces_embed_backend: {embed_backend!r}")

    def _load_adaface(self, cache_dir: str):
        try:
            import onnxruntime as ort
        except Exception as e:  # pragma: no cover - optional dep
            raise RuntimeError(
                "AdaFace backend needs onnxruntime (pip install onnxruntime); "
                f"import failed: {e}")
        mp = adaface_model_path(cache_dir)
        if not mp.is_file():
            raise RuntimeError(
                f"AdaFace model missing at {mp}. Regenerate it with "
                "tools/adaface_export.py, or set faces_embed_backend='sface'.")
        so = ort.SessionOptions()
        so.intra_op_num_threads = os.cpu_count() or 4
        return ort.InferenceSession(str(mp), so, providers=["CPUExecutionProvider"])

    def _embed(self, aligned_bgr):
        """112x112 aligned BGR crop -> L2-normalized float32 embedding."""
        if self._ada is not None:
            # AdaFace preprocessing: BGR, /255, then (x-0.5)/0.5 -> [-1,1], NCHW.
            x = ((aligned_bgr.astype("float32") / 255.0) - 0.5) / 0.5
            x = x.transpose(2, 0, 1)[None]
            feat = self._ada.run(None, {"input": x})[0].reshape(-1)
        else:
            feat = self._rec.feature(aligned_bgr).reshape(-1)
        feat = feat.astype("float32")
        n = float(np.linalg.norm(feat))
        return None if n == 0.0 else feat / n

    # -- image loading ----------------------------------------------------
    def load_bgr(self, path: str):
        """(bgr_array, scale) where scale = detected_size / original_size ≤ 1.

        Loads via Pillow (so HEIC and EXIF orientation are handled, matching the
        thumbnailer) and downscales the long side to ``max_side`` for speed.
        Read-only over the original.
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
            # JPEG (by 1/2, 1/4, 1/8) — a ~3-4x speedup on load for the large
            # photos that dominate this archive. Must run before any pixel
            # access (i.e. before exif_transpose). No-op for non-JPEG.
            #
            # Crucially, draft() silently shrinks `im.size` as a side effect: a
            # 4032x3024 photo becomes 2016x1512 before we ever compute a scale
            # factor. If the scale below were derived from the post-draft size,
            # it would only correct for the *second* resize and leave the boxes
            # off by whatever factor draft() already applied — landing crops on
            # the wrong region of the (still full-resolution) original. Basing
            # `scale` on `orig_side` (captured above, before draft runs) keeps
            # it correct regardless of what draft() did.
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
        # scale is expressed against the TRUE original side (not the post-draft
        # one) so callers can map detection boxes back to real image pixels.
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

    def detect_report(self, img_bgr, scale: float = 1.0,
                      *, apply_quality_gate: bool = True) -> DetectionReport:
        h, w = img_bgr.shape[:2]
        self._det.setInputSize((w, h))
        _, rows = self._det.detect(img_bgr)
        report = DetectionReport()
        if rows is None:
            return report
        inv = 1.0 / scale if scale else 1.0
        for row in rows:
            report.candidates += 1
            score = float(row[-1])
            if score < self.min_score:
                report.rejected["score"] += 1
                continue
            bx, by, bw, bh = (float(v) for v in row[:4])
            # min_px is judged in ORIGINAL pixels (what the user actually sees).
            if min(bw, bh) * inv < self.min_px:
                report.rejected["size"] += 1
                continue
            clipped = self._clipped_fraction(bx, by, bw, bh, w, h)
            if apply_quality_gate and clipped > self.max_clipped_fraction:
                report.rejected["clipped"] += 1
                continue
            aligned = self._rec.alignCrop(img_bgr, row)
            quality = measure_face_quality(aligned, self.min_focus)
            if apply_quality_gate and quality.focus_score < self.min_focus:
                report.rejected["focus"] += 1
                continue
            if (apply_quality_gate
                    and quality.extreme_fraction > self.max_extreme_fraction):
                report.rejected["exposure"] += 1
                continue
            feat = self._embed(aligned)
            if feat is None:
                continue
            report.faces.append(Face(
                x=max(0, round(bx * inv)), y=max(0, round(by * inv)),
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
