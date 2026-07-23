"""OpenCV YOLOX animal detector and deterministic pet-crop descriptors.

The OpenCV Zoo YOLOX-S model is downloaded once into the application cache.
Inference is entirely local. The implementation follows OpenCV Zoo's reference
pre/post-processing and keeps only configured pet species plus ``teddy bear``
as non-human context for the People pipeline.
"""

from __future__ import annotations

import os
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

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

COCO_CLASSES = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep",
    "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard",
    "sports ball", "kite", "baseball bat", "baseball glove", "skateboard",
    "surfboard", "tennis racket", "bottle", "wine glass", "cup", "fork",
    "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
    "couch", "potted plant", "bed", "dining table", "toilet", "tv",
    "laptop", "mouse", "remote", "keyboard", "cell phone", "microwave",
    "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase",
    "scissors", "teddy bear", "hair drier", "toothbrush",
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
            raise OSError(
                f"downloaded {MODEL_NAME} is too small ({size} bytes)")
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
    embedding: "np.ndarray"


def crop_descriptor(crop_bgr) -> "np.ndarray":
    """Compact color/texture descriptor for conservative within-species grouping."""
    if crop_bgr is None or crop_bgr.size == 0:
        return np.zeros(320, dtype="float32")
    square = cv2.resize(crop_bgr, (64, 64), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(square, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [16, 4], [0, 180, 0, 256]).reshape(-1)
    hist = hist.astype("float32")
    hist /= float(hist.sum()) + 1e-9
    gray = cv2.cvtColor(square, cv2.COLOR_BGR2GRAY)
    texture = cv2.resize(gray, (16, 16), interpolation=cv2.INTER_AREA).reshape(-1)
    texture = (texture.astype("float32") - float(texture.mean())) / 255.0
    texture /= float(np.linalg.norm(texture)) + 1e-9
    vector = np.concatenate([hist * 0.65, texture * 0.35])
    norm = float(np.linalg.norm(vector))
    return vector if norm == 0.0 else vector / norm


class PetBackend:
    input_size = (640, 640)
    strides = (8, 16, 32)

    def __init__(self, cache_dir: str, *, min_score: float = 0.60,
                 min_px: int = 48, max_side: int = 1280, species=(),
                 model_source="opencv-yolox-s-2022nov", log=None):
        if not available():
            raise RuntimeError("pet detection needs OpenCV DNN and NumPy")
        self.min_score = float(min_score)
        self.min_px = int(min_px)
        self.max_side = int(max_side)
        self.species = set(species) | {"teddy bear"}
        self.model_source = model_source
        self.net = cv2.dnn.readNet(str(ensure_model(cache_dir, log=log)))
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
        return (cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR),
                loaded_w / max(1, original_w), loaded_h / max(1, original_h))

    def detect(self, image_bgr) -> list[AnimalDetection]:
        original_h, original_w = image_bgr.shape[:2]
        rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
        ratio = min(
            self.input_size[0] / max(1, original_w),
            self.input_size[1] / max(1, original_h))
        resized_w, resized_h = (
            max(1, round(original_w * ratio)),
            max(1, round(original_h * ratio)))
        padded = np.full(
            (self.input_size[1], self.input_size[0], 3), 114,
            dtype="float32")
        padded[:resized_h, :resized_w] = cv2.resize(
            rgb, (resized_w, resized_h), interpolation=cv2.INTER_LINEAR)
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
        eligible = [
            i for i, class_id in enumerate(class_ids)
            if confidences[i] >= self.min_score
            and COCO_CLASSES[int(class_id)] in self.species
        ]
        if not eligible:
            return []
        candidate_boxes = boxes[eligible].tolist()
        candidate_scores = confidences[eligible].tolist()
        candidate_classes = class_ids[eligible].tolist()
        if hasattr(cv2.dnn, "NMSBoxesBatched"):
            keep = cv2.dnn.NMSBoxesBatched(
                candidate_boxes, candidate_scores, candidate_classes,
                self.min_score, 0.50)
        else:  # OpenCV 4.8 compatibility: perform NMS independently per class.
            kept = []
            for class_id in set(candidate_classes):
                local = [index for index, value in enumerate(candidate_classes)
                         if value == class_id]
                selected = cv2.dnn.NMSBoxes(
                    [candidate_boxes[index] for index in local],
                    [candidate_scores[index] for index in local],
                    self.min_score, 0.50)
                kept.extend(local[int(index)]
                            for index in np.asarray(selected).reshape(-1))
            keep = kept
        out = []
        for local_index in np.asarray(keep).reshape(-1):
            source_index = eligible[int(local_index)]
            x, y, width, height = boxes[source_index]
            x = max(0, round(float(x) / ratio))
            y = max(0, round(float(y) / ratio))
            width = min(original_w - x, round(float(width) / ratio))
            height = min(original_h - y, round(float(height) / ratio))
            if min(width, height) < self.min_px:
                continue
            crop = image_bgr[y:y + height, x:x + width]
            out.append(AnimalDetection(
                species=COCO_CLASSES[int(class_ids[source_index])],
                x=x, y=y, w=width, h=height,
                score=float(confidences[source_index]),
                embedding=crop_descriptor(crop),
            ))
        return out

    def process_path(self, path: str) -> list[AnimalDetection]:
        image, scale_x, scale_y = self.load_bgr(path)
        detections = self.detect(image)
        for detection in detections:
            detection.x = round(detection.x / scale_x)
            detection.y = round(detection.y / scale_y)
            detection.w = round(detection.w / scale_x)
            detection.h = round(detection.h / scale_y)
        return detections
