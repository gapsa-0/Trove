"""Configuration: source roots, data/cache locations, ignore rules, thresholds.

Mutable state lives in the current user's application-data directory. An optional
``config.json`` there overrides the defaults.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

# Project layout ------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
from .paths import (app_data_dir, config_file, default_cache_dir,
                    default_db_path, ensure_app_data_dirs)


def _load_dotenv() -> None:
    """Load local development secrets without adding a runtime dependency.

    Explicit environment variables win, so deployment environments can still
    provide credentials without relying on a file.
    """
    dotenv = PROJECT_ROOT / ".env"
    if not dotenv.is_file():
        return
    for raw in dotenv.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


_load_dotenv()

# The archive to catalog (read-only). Multiple roots are supported.
DEFAULT_ROOTS: list[str] = []

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
    roots: list[str] = field(default_factory=list)
    db_path: str = field(default_factory=lambda: str(default_db_path()))
    cache_dir: str = field(default_factory=lambda: str(default_cache_dir()))

    # Semantic Browse search (Voyage Multimodal 3.5). The API key is deliberately
    # not a config field: put VOYAGE_API_KEY in the ignored project-root .env
    # file (or process environment), never data/config.json.
    semantic_embedding_model: str = "voyage-multimodal-3.5"
    semantic_embedding_dimensions: int = 1024
    semantic_inline_max_bytes: int = 20 * 1024 * 1024
    # Hide weak semantic matches. This is cosine similarity, so valid values
    # range from -1 to 1; tune it upward for fewer, more precise results.
    semantic_search_min_similarity: float = 0.25

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
    # Extraction-time quality gate. Metrics are measured on the aligned 112px
    # crop, making them comparable across original image sizes. Rejections are
    # counted in face_scan so thresholds can be calibrated without retaining
    # false detections as people.
    faces_min_focus: float = 35.0    # variance of the grayscale Laplacian
    faces_max_extreme_fraction: float = 0.80  # pixels near black or white
    faces_max_clipped_fraction: float = 0.18  # bbox area outside decoded image
    faces_quality_version: str = "opencv-laplacian-v1"
    # Pets / non-human face filtering. YOLOX runs locally over canonical images
    # and supplies animal regions before the human-face pass. Identity grouping
    # uses a deterministic crop descriptor; thresholds are intentionally
    # conservative because users can merge/name pets but originals never change.
    pets_min_score: float = 0.60
    pets_min_px: int = 48
    pets_max_side: int = 1280
    pets_species: list[str] = field(
        default_factory=lambda: ["cat", "dog", "bird", "horse"])
    pets_cluster_similarity: float = 0.94
    pets_min_detections: int = 2
    pets_face_overlap: float = 0.60
    pets_model_version: str = "opencv-yolox-s-2022nov"
    faces_nonhuman_min_examples: int = 3
    faces_nonhuman_min_similarity: float = 0.78
    faces_nonhuman_similarity_margin: float = 0.08
    # Clustering into people is two-stage (faces/cluster.py). Stage 1 over-clusters
    # into small, PURE fragments via a *mutual k-NN* graph: link two faces only
    # when each is among the other's `faces_knn_k` most-similar faces AND their
    # cosine similarity is >= `faces_link_sim`. Stage 2 merges fragment centroids
    # with *average* linkage while their mean cosine similarity is >= `faces_merge_sim`.
    # A final person needs `faces_min_faces`.
    #
    # Why mutual k-NN and not "union every pair >= faces_link_sim": that plain
    # threshold is single-linkage, so one spurious bridge face (blurry / profile /
    # a false-positive detection weakly similar to two different people) fuses
    # both their components. On this archive it percolated into ONE blob holding
    # ~40% of all faces even at a threshold high enough to start splitting true
    # identities (measured intra-blob cosine ~0.15 = pure noise). Capping each
    # face to its k best neighbours and requiring reciprocity strips those bridge
    # edges into small, pure fragments.
    #
    # Why AVERAGE linkage in stage 2 and not complete: complete linkage needs
    # *every* cross pair within threshold, so a high-variance identity (same
    # person young vs old, many poses) never coalesces — the most-photographed
    # person split into ~30 clusters. Average linkage keys on the mean cross-pair,
    # tolerating that spread, and can't chain here thanks to a wide margin measured
    # on this archive: different people's centroids are <=~0.30 cosine while one
    # person's sub-clusters are ~0.75-0.97. That gap is why faces_merge_sim can be
    # pushed down to ~0.40 to reunite even the harder splits without merging
    # distinct people (empirically the biggest cluster grows only ~8% from 0.52 to
    # 0.40 — reuniting selves, not fusing others). Lower still (~0.35) merges a few
    # more but starts risking look-alikes; leave the last stubborn splits to a
    # manual GUI merge. AdaFace numbers; for SFace's tighter spread use higher sims.
    # Stage 3 re-merges whole clusters on their CENTROID direction (cosine of the
    # normalized cluster means), which divides out within-cluster spread. It catches
    # the case stage 2's mean-cross-pair metric misses: a tight cluster and a loose
    # cluster of the same person (centroids ~0.62) whose mean cross-pair (~0.36) fell
    # under faces_merge_sim. 0.55 sits far above different people's centroids (~0.30),
    # so it reunites split selves without fusing distinct people (validated: zero
    # collisions among named people).
    faces_knn_k: int = 5              # stage-1 mutual-kNN neighbours per face
    faces_link_sim: float = 0.50      # stage-1 mutual-kNN similarity floor (cosine)
    faces_merge_sim: float = 0.40     # stage-2 avg-linkage merge (mean cosine sim)
    faces_centroid_merge_sim: float = 0.55  # stage-3 centroid-direction merge (cosine)
    faces_min_faces: int = 3          # min faces for a cluster to become a person

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
        path = config_file()
        if path.exists():
            data = json.loads(path.read_text())
            for k, v in data.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
        return cfg

    def save(self) -> None:
        app_data_dir().mkdir(parents=True, exist_ok=True)
        config_file().write_text(json.dumps(asdict(self), indent=2))

    def ensure_dirs(self) -> None:
        ensure_app_data_dirs()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        cache_dir = Path(self.cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "thumbs").mkdir(parents=True, exist_ok=True)
        (cache_dir / "models").mkdir(parents=True, exist_ok=True)
