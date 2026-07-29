"""OpenCV YOLOX animal detector and deterministic pet-crop descriptors.

The OpenCV Zoo YOLOX-S model is downloaded once into the application cache.
Inference is entirely local. The implementation follows OpenCV Zoo's reference
pre/post-processing and keeps only configured pet species plus ``teddy bear``
as non-human context for the People pipeline.

The same forward pass also reports COCO ``person`` boxes (at a lower floor, and
never as pets). They cost nothing extra and are the archive's human signal: a
human who is not vertical in the frame — lying down, or a whole photo stored
sideways — is reliably misread by YOLOX as ``dog``, and a ``person`` box over
the same region is what tells the fused detect stage it is a human, not a pet.
"""

from __future__ import annotations

import os
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .. import runtime

try:
    import cv2
    import numpy as np
except Exception:  # pragma: no cover - optional dependency
    cv2 = None
    np = None

MODEL_NAME = "object_detection_yolox_2022nov.onnx"
MODEL_URL = (
    "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/"
    f"models/object_detection_yolox/{MODEL_NAME}"
)
MODEL_MIN_BYTES = 20_000_000

# Individual-animal re-identification embedder: a self-exported ONNX of the
# AvitoTech DINOv2-small model fine-tuned for cat/dog re-ID (384-d CLS token).
# Not distributed via a URL like the OpenCV models — regenerate it with
# tools/build/dinov2_pet_export.py if missing. Replaces the old hand-crafted HSV
# colour/texture descriptor: a learned embedding groups the SAME animal across
# poses/lighting far better than colour statistics.
DINOV2_SUBDIR = "dinov2_pet"
DINOV2_MODEL = "dinov2_pet.onnx"
# DINOv2 preprocessing: RGB, resize 224, /255, ImageNet mean/std normalization.
_IMAGENET_MEAN = (0.485, 0.456, 0.406)
_IMAGENET_STD = (0.229, 0.224, 0.225)


def dinov2_model_path(cache_dir: str) -> Path:
    """Where the re-ID ONNX lives: the frozen build's copy first, else the cache.

    A packaged build carries this model (it has no upstream URL to fetch it from),
    so the bundled copy wins; a source checkout falls back to the exported file in
    the cache directory.
    """
    bundled = runtime.bundled_model(f"{DINOV2_SUBDIR}/{DINOV2_MODEL}")
    if bundled is not None:
        return bundled
    return Path(cache_dir) / "models" / DINOV2_SUBDIR / DINOV2_MODEL


def dinov2_ready(cache_dir: str) -> bool:
    p = dinov2_model_path(cache_dir)
    return p.is_file() and p.stat().st_size > 1_000_000


COCO_CLASSES = (
    "person",
    "bicycle",
    "car",
    "motorcycle",
    "airplane",
    "bus",
    "train",
    "truck",
    "boat",
    "traffic light",
    "fire hydrant",
    "stop sign",
    "parking meter",
    "bench",
    "bird",
    "cat",
    "dog",
    "horse",
    "sheep",
    "cow",
    "elephant",
    "bear",
    "zebra",
    "giraffe",
    "backpack",
    "umbrella",
    "handbag",
    "tie",
    "suitcase",
    "frisbee",
    "skis",
    "snowboard",
    "sports ball",
    "kite",
    "baseball bat",
    "baseball glove",
    "skateboard",
    "surfboard",
    "tennis racket",
    "bottle",
    "wine glass",
    "cup",
    "fork",
    "knife",
    "spoon",
    "bowl",
    "banana",
    "apple",
    "sandwich",
    "orange",
    "broccoli",
    "carrot",
    "hot dog",
    "pizza",
    "donut",
    "cake",
    "chair",
    "couch",
    "potted plant",
    "bed",
    "dining table",
    "toilet",
    "tv",
    "laptop",
    "mouse",
    "remote",
    "keyboard",
    "cell phone",
    "microwave",
    "oven",
    "toaster",
    "sink",
    "refrigerator",
    "book",
    "clock",
    "vase",
    "scissors",
    "teddy bear",
    "hair drier",
    "toothbrush",
)


def available() -> bool:
    return cv2 is not None and np is not None and hasattr(cv2, "dnn")


def model_path(cache_dir: str) -> Path:
    return Path(cache_dir) / "models" / "pets" / MODEL_NAME


def models_ready(cache_dir: str) -> bool:
    path = model_path(cache_dir)
    return path.is_file() and path.stat().st_size >= MODEL_MIN_BYTES


def ensure_model(cache_dir: str, log=None) -> Path:
    path = model_path(cache_dir)
    if models_ready(cache_dir):
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    if log:
        log(f"downloading pet detector {MODEL_NAME} …")
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".part")
    os.close(fd)
    try:
        urllib.request.urlretrieve(MODEL_URL, temporary)
        size = os.path.getsize(temporary)
        if size < MODEL_MIN_BYTES:
            raise OSError(f"downloaded {MODEL_NAME} is too small ({size} bytes)")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
    return path


@dataclass
class AnimalDetection:
    species: str
    x: int
    y: int
    w: int
    h: int
    score: float
    embedding: np.ndarray


@dataclass
class HumanDetection:
    """A COCO ``person`` box: context for the face/pet cross-check, never a pet.

    Kept deliberately separate from ``AnimalDetection`` (no species, no re-ID
    embedding) so it cannot be written to ``animal_detections`` by accident.
    """

    x: int
    y: int
    w: int
    h: int
    score: float


def _load_dinov2(cache_dir: str):
    """Create the onnxruntime session for the DINOv2 pet-reID embedder."""
    try:
        import onnxruntime as ort
    except Exception as e:  # pragma: no cover - optional dep
        raise RuntimeError(
            f"pet re-ID needs onnxruntime (pip install onnxruntime); import failed: {e}"
        ) from e
    mp = dinov2_model_path(cache_dir)
    if not mp.is_file():
        raise RuntimeError(
            f"DINOv2 pet model missing at {mp}. Regenerate it with "
            "tools/build/dinov2_pet_export.py."
        )
    so = ort.SessionOptions()
    so.intra_op_num_threads = os.cpu_count() or 4
    return ort.InferenceSession(str(mp), so, providers=["CPUExecutionProvider"])


class PetBackend:
    input_size = (640, 640)
    strides = (8, 16, 32)

    def __init__(
        self,
        cache_dir: str,
        *,
        min_score: float = 0.60,
        min_px: int = 48,
        max_side: int = 1280,
        species=(),
        human_min_score: float = 0.20,
        model_source="opencv-yolox-s-2022nov",
        log=None,
    ):
        if not available():
            raise RuntimeError("pet detection needs OpenCV DNN and NumPy")
        self.min_score = float(min_score)
        self.min_px = int(min_px)
        self.max_side = int(max_side)
        # `person` is only ever context for the cross-check, so its floor is well
        # below min_score: a weak person box over an animal box is already strong
        # evidence the "animal" is a misread human.
        self.human_min_score = float(human_min_score)
        self.species = set(species) | {"teddy bear"}
        self.model_source = model_source
        self.net = cv2.dnn.readNet(str(ensure_model(cache_dir, log=log)))
        self._dino = _load_dinov2(cache_dir)
        self._mean = np.array(_IMAGENET_MEAN, dtype="float32")
        self._std = np.array(_IMAGENET_STD, dtype="float32")
        grids, expanded = [], []
        for stride in self.strides:
            hsize = self.input_size[1] // stride
            wsize = self.input_size[0] // stride
            xv, yv = np.meshgrid(np.arange(wsize), np.arange(hsize))
            grid = np.stack((xv, yv), 2).reshape(1, -1, 2)
            grids.append(grid)
            expanded.append(np.full((*grid.shape[:2], 1), stride))
        self.grids = np.concatenate(grids, axis=1)
        self.expanded_strides = np.concatenate(expanded, axis=1)

    def load_bgr(self, path: str):
        from PIL import Image, ImageOps

        try:
            import pillow_heif

            pillow_heif.register_heif_opener()
        except Exception:
            pass
        with Image.open(path) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            original_w, original_h = image.size
            image.thumbnail((self.max_side, self.max_side))
            rgb = np.asarray(image)
        loaded_h, loaded_w = rgb.shape[:2]
        return (
            cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR),
            loaded_w / max(1, original_w),
            loaded_h / max(1, original_h),
        )

    def detect(self, image_bgr) -> list[AnimalDetection]:
        """Pet-species boxes only (unchanged contract for the standalone path)."""
        return self.detect_with_humans(image_bgr)[0]

    def detect_with_humans(self, image_bgr):
        """``(animals, humans)`` from ONE forward pass.

        ``humans`` are COCO ``person`` boxes kept at ``human_min_score`` purely as
        cross-check context — they carry no re-ID embedding (DINOv2 only runs on
        animal crops) and are never persisted as pets.
        """
        animals, humans = [], []
        for species, box, score in self._forward(image_bgr):
            if species == "person":
                humans.append(HumanDetection(*box, score=score))
                continue
            x, y, width, height = box
            if min(width, height) < self.min_px:
                continue
            animals.append(
                AnimalDetection(
                    species=species,
                    x=x,
                    y=y,
                    w=width,
                    h=height,
                    score=score,
                    embedding=self._embed_crop(image_bgr[y : y + height, x : x + width]),
                )
            )
        return animals, humans

    def detect_humans(self, image_bgr) -> list[HumanDetection]:
        """Person boxes only — no animal crops, so no DINOv2 work.

        Used for the quarter-turn re-test, where the animal boxes of the rotated
        frame are of no interest and embedding them would be pure waste.
        """
        return [
            HumanDetection(*box, score=score)
            for species, box, score in self._forward(image_bgr)
            if species == "person"
        ]

    def _forward(self, image_bgr):
        """One YOLOX pass -> ``(species, (x, y, w, h), score)`` in image pixels.

        Boxes are already NMS'd and mapped back out of the letterboxed 640x640
        input; ``min_px`` and re-ID embedding are the callers' business.
        """
        original_h, original_w = image_bgr.shape[:2]
        rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
        ratio = min(
            self.input_size[0] / max(1, original_w), self.input_size[1] / max(1, original_h)
        )
        resized_w, resized_h = (
            max(1, round(original_w * ratio)),
            max(1, round(original_h * ratio)),
        )
        padded = np.full((self.input_size[1], self.input_size[0], 3), 114, dtype="float32")
        padded[:resized_h, :resized_w] = cv2.resize(
            rgb, (resized_w, resized_h), interpolation=cv2.INTER_LINEAR
        )
        blob = padded.transpose(2, 0, 1)[None]
        self.net.setInput(blob)
        output = self.net.forward(self.net.getUnconnectedOutLayersNames())[0]
        dets = output[0].copy()
        dets[:, :2] = (dets[:, :2] + self.grids[0]) * self.expanded_strides[0]
        dets[:, 2:4] = np.exp(dets[:, 2:4]) * self.expanded_strides[0]
        boxes = np.empty_like(dets[:, :4])
        boxes[:, 0] = dets[:, 0] - dets[:, 2] / 2
        boxes[:, 1] = dets[:, 1] - dets[:, 3] / 2
        boxes[:, 2:4] = dets[:, 2:4]
        scores = dets[:, 4:5] * dets[:, 5:]
        class_ids = np.argmax(scores, axis=1)
        confidences = np.max(scores, axis=1)
        # Two floors in one pass: pet species at min_score, `person` lower. NMS
        # applies its own score threshold, so it has to run at the lower of the
        # two or every weak person box would be discarded there. NMS is per
        # class either way, so the added person candidates cannot change which
        # animal boxes survive.
        nms_floor = min(self.min_score, self.human_min_score)
        eligible = [
            i
            for i, class_id in enumerate(class_ids)
            if (COCO_CLASSES[int(class_id)] in self.species and confidences[i] >= self.min_score)
            or (COCO_CLASSES[int(class_id)] == "person" and confidences[i] >= self.human_min_score)
        ]
        if not eligible:
            return []
        candidate_boxes = boxes[eligible].tolist()
        candidate_scores = confidences[eligible].tolist()
        candidate_classes = class_ids[eligible].tolist()
        if hasattr(cv2.dnn, "NMSBoxesBatched"):
            keep = cv2.dnn.NMSBoxesBatched(
                candidate_boxes, candidate_scores, candidate_classes, nms_floor, 0.50
            )
        else:  # OpenCV 4.8 compatibility: perform NMS independently per class.
            kept = []
            for class_id in set(candidate_classes):
                local = [
                    index for index, value in enumerate(candidate_classes) if value == class_id
                ]
                selected = cv2.dnn.NMSBoxes(
                    [candidate_boxes[index] for index in local],
                    [candidate_scores[index] for index in local],
                    nms_floor,
                    0.50,
                )
                kept.extend(local[int(index)] for index in np.asarray(selected).reshape(-1))
            keep = kept
        out = []
        for local_index in np.asarray(keep).reshape(-1):
            source_index = eligible[int(local_index)]
            x, y, width, height = boxes[source_index]
            x = max(0, round(float(x) / ratio))
            y = max(0, round(float(y) / ratio))
            width = min(original_w - x, round(float(width) / ratio))
            height = min(original_h - y, round(float(height) / ratio))
            out.append(
                (
                    COCO_CLASSES[int(class_ids[source_index])],
                    (x, y, width, height),
                    float(confidences[source_index]),
                )
            )
        return out

    def _embed_crop(self, crop_bgr) -> np.ndarray:
        """384-d L2-normalized DINOv2 re-ID embedding of an animal crop.

        Cosine similarity between two crops of the same individual is high; the
        embedding is unit-normalized so clustering can use a dot product.
        """
        if crop_bgr is None or crop_bgr.size == 0:
            return np.zeros(384, dtype="float32")
        rgb = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB)
        rgb = cv2.resize(rgb, (224, 224), interpolation=cv2.INTER_AREA)
        x = (rgb.astype("float32") / 255.0 - self._mean) / self._std
        x = x.transpose(2, 0, 1)[None]  # (1,3,224,224) NCHW
        feat = self._dino.run(None, {"input": x})[0].reshape(-1).astype("float32")
        n = float(np.linalg.norm(feat))
        return feat if n == 0.0 else feat / n

    def process_path(self, path: str) -> list[AnimalDetection]:
        image, scale_x, scale_y = self.load_bgr(path)
        detections = self.detect(image)
        for detection in detections:
            detection.x = round(detection.x / scale_x)
            detection.y = round(detection.y / scale_y)
            detection.w = round(detection.w / scale_x)
            detection.h = round(detection.h / scale_y)
        return detections
