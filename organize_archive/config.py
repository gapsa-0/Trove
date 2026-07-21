"""Configuration: source roots, data/cache locations, ignore rules, thresholds.

Defaults keep everything self-contained inside the project folder (data/), so
nothing is ever written among the originals. An optional JSON file at
``data/config.json`` overrides the defaults.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

# Project layout ------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_DB_PATH = DATA_DIR / "archive.db"
DEFAULT_CACHE_DIR = DATA_DIR / "cache"
CONFIG_FILE = DATA_DIR / "config.json"

# The archive to catalog (read-only). Multiple roots are supported.
DEFAULT_ROOTS = ["/media/capsa/Residuos/Multimedia"]

# Files that are not media content. Google Takeout ``.json`` sidecars are
# excluded here as *content* — they are consumed as metadata in Phase 3.
IGNORE_EXTENSIONS = {
    "json", "db", "thm", "ini", "nomedia", "part", "tmp",
}
IGNORE_FILENAMES = {
    "thumbs.db", "desktop.ini", ".nomedia", ".picasa.ini",
    "picasa.ini", ".ds_store",
}
# Substrings that mark Google/Picasa/Android index & housekeeping leftovers
# (these often have no extension, e.g. "thumbdata3-123", "nomedia_1620517712...").
IGNORE_NAME_SUBSTRINGS = (
    "thumbindex", "thumbdata", "database_uuid", "nomedia",
    ".com.google.chrome.",  # browser temp download leftovers
)


@dataclass
class Config:
    roots: list[str] = field(default_factory=lambda: list(DEFAULT_ROOTS))
    db_path: str = str(DEFAULT_DB_PATH)
    cache_dir: str = str(DEFAULT_CACHE_DIR)

    # Hashing
    fast_hash_sample_bytes: int = 65536  # head+tail sample for the cheap prefilter

    # Dedup (Phase 4)
    phash_hamming_threshold: int = 6

    # Faces (Phase 6). Detection + embedding run locally; nothing leaves the
    # machine. Detection is YuNet; the face is aligned to the 112x112 ArcFace
    # template (OpenCV's alignCrop) and embedded by the selected backend.
    #   "adaface" -> AdaFace ir101 (WebFace12M), 512-d, ONNX/onnxruntime. Much
    #               stronger on this archive's varied/low-quality faces; the
    #               model file lives in cache/models/adaface/ (see faces/backend).
    #   "sface"   -> the original OpenCV SFace, 128-d. Lighter, weaker.
    # Switching backend changes the embedding dimension, so it requires a full
    # re-extract (wipe faces/face_scan) — the vectors are not comparable.
    faces_embed_backend: str = "adaface"
    faces_min_score: float = 0.70   # drop YuNet detections below this confidence
                                    # (real frontal faces score ~0.9; raising
                                    # this trims false positives on sky/texture)
    faces_min_px: int = 36          # drop faces smaller than this (box side, px)
    faces_max_side: int = 960       # downscale long side before detection (speed)
    # Clustering into people is two-stage (faces/cluster.py), because plain
    # DBSCAN chains distinct identities through low-quality "bridge" faces into
    # one giant blob. Stage 1 over-clusters tightly (link two faces only above
    # `faces_link_sim` cosine similarity → small, pure fragments); stage 2 merges
    # fragment centroids with complete linkage below `faces_merge_sim` distance
    # (complete linkage can't chain). A final person needs `faces_min_faces`.
    # Defaults below are calibrated for AdaFace (measured on this archive:
    # same-person cosine ~0.63-0.74, different-person ~0.05, max ~0.14 — a wide,
    # clean margin). For SFace's tighter distribution use ~0.76 / ~0.52.
    faces_link_sim: float = 0.50    # stage-1 tight over-cluster (cosine sim)
    faces_merge_sim: float = 0.40   # stage-2 centroid merge (cosine sim)
    faces_min_faces: int = 3        # min faces for a cluster to become a person

    # Date resolution priority (Phase 3). Tunable; first available wins.
    date_priority: list[str] = field(
        default_factory=lambda: ["takeout_json", "exif", "filename", "mtime"]
    )
    # IANA timezone (e.g. "America/Argentina/Buenos_Aires") used to convert
    # Google Takeout's UTC timestamps to local wall-clock time, so evening
    # photos don't roll into the next day. None => keep UTC.
    timezone: str | None = None

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text())
            for k, v in data.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
        return cfg

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(asdict(self), indent=2))

    def ensure_dirs(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.cache_dir).mkdir(parents=True, exist_ok=True)
