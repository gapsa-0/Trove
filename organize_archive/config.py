"""Configuration: source roots, data/cache locations, ignore rules, thresholds.

Mutable state lives in the current user's application-data directory. An optional
``config.json`` there overrides the defaults.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

# Project layout ------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
from .paths import (app_data_dir, config_file, default_cache_dir,
                    default_db_path, ensure_app_data_dirs,
                    archive_dir, archive_db_path, archive_cache_dir)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
    # ``roots``/``db_path``/``cache_dir`` remain the CLI's single shared catalog
    # (``oa scan``/``oa dedup``/... — a separate, unchanged tool). The GUI's
    # notion of an "archive" is fully isolated instead: each entry in
    # ``archives`` gets its own database and its own thumbnail/face-crop cache
    # under ``archive_dir(id)``, so nothing about one archive's content is
    # visible to, or reusable by, another. ``db_path``/``cache_dir`` still back
    # the app-wide *shared* resources (ML model weights, the app icon) that are
    # not archive content.
    roots: list[str] = field(default_factory=list)
    db_path: str = field(default_factory=lambda: str(default_db_path()))
    cache_dir: str = field(default_factory=lambda: str(default_cache_dir()))
    archives: list[dict] = field(default_factory=list)
    # Latches the one-time copy of a pre-existing shared catalog into the new
    # per-archive layout, so it runs at most once ever (see migrate_legacy_archive).
    legacy_migrated: bool = False
    # Whole-pipeline pause (GUI "Library health" panel). A single flag, not
    # per-archive: only one archive is ever open at a time. Persisted so a
    # paused background pipeline stays paused across an app restart.
    pipeline_paused: bool = False

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
    # machine. Detection is InsightFace SCRFD (det_10g); each face is aligned to
    # the 112x112 ArcFace template (5-point similarity transform) and embedded by
    # ArcFace (w600k_r50), a 512-d vector run via onnxruntime. Both ONNX files are
    # the buffalo_l pack, fetched once into cache/models/insightface/ (see
    # faces/backend). SCRFD replaced YuNet (fewer sky/wall false positives, better
    # small-face recall + landmarks); ArcFace replaced AdaFace/SFace. Changing the
    # embedder changes the vectors, so it requires a full re-extract.
    faces_det_size: int = 640        # SCRFD detector input square (bigger = better
                                     # small-face recall, slower)
    faces_min_score: float = 0.50    # accept faces at/above this SCRFD confidence
                                     # (SCRFD's own floor is ~0.5; real frontal
                                     # faces score ~0.8+). Animal faces are handled
                                     # by the pet cross-check, not this threshold.
    faces_min_px: int = 36           # drop faces smaller than this (box side, px)
    faces_max_side: int = 960        # standalone backend decode cap (the fused
                                     # detect stage decodes once at detect_max_side)
    faces_max_clipped_fraction: float = 0.18  # reject a box mostly outside the frame
    # Advisory quality metrics (focus/exposure) are still measured on the aligned
    # crop and stored per face for display/calibration, but no longer gate the live
    # path — SCRFD's confidence is the primary filter. Used by `oa faces --calibrate`.
    faces_min_focus: float = 35.0             # grayscale Laplacian variance (advisory)
    faces_max_extreme_fraction: float = 0.80  # near-black/near-white pixels (advisory)
    faces_quality_version: str = "opencv-laplacian-v1"

    # Fused detection (Phase 6/9). People (SCRFD) and animals (YOLOX) are found in
    # ONE pass: each image is decoded a single time at this resolution and both
    # detectors run on that array, so ~150k photos are decoded once, not twice.
    detect_max_side: int = 1280      # long-side cap for the single shared decode

    # Pets. YOLOX detects animal regions locally; identities are grouped from a
    # DINOv2 re-ID embedding of each crop (cache/models/dinov2_pet/). The animal
    # boxes also cross-check the face pass: a face mostly inside an animal box is
    # dropped from People (the one non-human rule, applied inline in detect/).
    pets_min_score: float = 0.60
    pets_min_px: int = 48
    pets_max_side: int = 1280        # standalone backend decode cap (see detect_max_side)
    pets_species: list[str] = field(
        default_factory=lambda: ["cat", "dog", "bird", "horse"])
    pets_cluster_similarity: float = 0.75  # DINOv2 cosine (calibrated on-archive:
                                            # same-animal ~0.8-0.96, different ≤~0.3;
                                            # complete-link keeps 0.75 conservative)
    pets_min_detections: int = 2
    pets_face_overlap: float = 0.60  # face-in-animal overlap that marks a non-human
    # Human cross-check. The same YOLOX pass reports COCO `person` boxes at
    # `pets_human_min_score` (well under pets_min_score — a weak person box over
    # an animal box is already strong evidence). An animal box whose IoU with one
    # reaches `pets_human_iou` is a misclassified human, not a pet: YOLOX calls a
    # person who is not vertical in the frame — lying down, or a whole photo
    # stored sideways — a `dog` with real confidence. IoU (same object), not
    # containment, so a person *holding* a pet never vetoes it.
    pets_human_min_score: float = 0.20
    # Calibrated on-archive: a person misread as a dog produces a box on top of
    # its own person box (IoU 0.95-0.97), while a person *holding* a pet only
    # reaches 0.20-0.63 even when the animal fills their arms. 0.80 separates the
    # two with room on both sides; below ~0.7 it starts eating held pets.
    pets_human_iou: float = 0.80
    pets_model_version: str = "yolox-s-2022nov+dinov2s-petreid-v2-humanveto"

    # True orientation (detect/extract.py). Many photos here are stored with
    # their pixels turned while EXIF claims otherwise, which blinds every model
    # in the pipeline. A turn that yields a confident face (faces_min_score)
    # settles it; when the copy is too degraded for SCRFD at any angle, a YOLOX
    # `person` reading is the fallback and needs both an absolute score and a
    # clear margin over upright, because person scores vary far less between
    # turns than face scores do.
    # Faces that must resolve at the same quarter turn before that turn is
    # believed. Several people are never all lying down the same way, so a quorum
    # is decisive; a lone rotated face is nearly always a doll, a cake figurine
    # or someone lying down (measured on-archive), and two was still thin — a
    # meme collage produced two weak ones. Three separates every case seen here.
    orientation_min_faces: int = 3
    orientation_person_min: float = 0.75
    orientation_person_margin: float = 0.25
    # Share of the frame a lone subject must cover before its own angle is taken
    # to be the photo's. Someone lying on the grass in a landscape shot reads
    # exactly like a standing person in a sideways one; the difference is that
    # the sideways portrait's subject fills the frame (measured on-archive: 0.93)
    # while the person lying down is a detail of a scene (0.03).
    orientation_min_subject: float = 0.35
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
    # Day-vs-month order to assume for a numeric D-M-Y filename date that is
    # genuinely ambiguous (both numbers <= 12, no forcing value on either
    # side). Forced cases (either number > 12) resolve unambiguously
    # regardless of this flag; this is only the tie-break fallback.
    filename_date_day_first: bool = True

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
        Path(self.cache_dir).mkdir(parents=True, exist_ok=True)

    # -- per-archive isolation ---------------------------------------------
    # Each GUI archive is a fully self-contained store: its own database, its
    # own thumbnail/face-crop cache. Only app-wide binaries (ML model weights,
    # the app icon — see db_path/cache_dir above) are shared between archives.

    def _next_archive_id(self) -> int:
        return max((a["id"] for a in self.archives), default=0) + 1

    def archive_path(self, archive_id: int) -> str | None:
        return next((a["path"] for a in self.archives if a["id"] == archive_id), None)

    def archive_db_path(self, archive_id: int) -> str:
        return str(archive_db_path(archive_id))

    def archive_cache_dir(self, archive_id: int) -> str:
        return str(archive_cache_dir(archive_id))

    def add_archive_entry(self, path: str) -> dict:
        """Register a new isolated archive and create its private directory."""
        aid = self._next_archive_id()
        archive_dir(aid).mkdir(parents=True, exist_ok=True)
        entry = {"id": aid, "path": path, "added_at": _now_iso()}
        self.archives.append(entry)
        self.save()
        return entry

    def remove_archive_entry(self, archive_id: int) -> None:
        self.archives = [a for a in self.archives if a["id"] != archive_id]
        self.save()

    def migrate_legacy_archive(self) -> None:
        """One-time copy of the old shared single-catalog database into the new
        per-archive layout, so a catalog built before this version doesn't
        appear to vanish from the GUI. Runs at most once (``legacy_migrated``
        latches even when there was nothing to migrate).

        Only handles the common case of a legacy database with exactly one
        root — that is what every existing installation actually has. A legacy
        database with several roots predates per-archive isolation entirely
        and mixed their data in ways that can't be split back apart
        automatically (e.g. cross-root duplicate groups); migration is skipped
        and the folders must be re-added as separate archives instead.
        """
        if self.legacy_migrated:
            return
        self.legacy_migrated = True
        legacy_db = Path(self.db_path)
        if not legacy_db.is_file():
            self.save()
            return
        try:
            src = sqlite3.connect(str(legacy_db))
        except sqlite3.DatabaseError:
            self.save()
            return
        try:
            try:
                roots = src.execute("SELECT id, path FROM roots").fetchall()
            except sqlite3.DatabaseError:
                roots = []
            if len(roots) == 1:
                _legacy_root_id, root_path = roots[0]
                aid = self._next_archive_id()
                archive_dir(aid).mkdir(parents=True, exist_ok=True)
                # A plain file copy could miss pages the legacy database hasn't
                # checkpointed out of its WAL yet if it's open elsewhere (e.g. a
                # running GUI). Back it up through SQLite instead, so the copy is
                # always a consistent snapshot regardless of concurrent access.
                dst = sqlite3.connect(str(archive_db_path(aid)))
                try:
                    src.backup(dst)
                finally:
                    dst.close()
                legacy_cache = Path(self.cache_dir)
                for sub in ("thumbs", "faces"):
                    cache_src = legacy_cache / sub
                    if cache_src.is_dir():
                        # dirs_exist_ok: this whole method must stay safe to
                        # re-run (see the hard "resumable & idempotent" rule)
                        # -- a prior attempt may have partially copied before
                        # crashing or being interrupted, leaving the target
                        # directory already present.
                        shutil.copytree(cache_src, archive_cache_dir(aid) / sub,
                                         dirs_exist_ok=True)
                self.archives.append({"id": aid, "path": root_path, "added_at": _now_iso()})
            elif len(roots) > 1:
                print(f"Note: {legacy_db} holds {len(roots)} roots from the previous "
                      "shared-catalog design. Each archive now needs its own database, "
                      "so automatic migration was skipped; re-add those folders as "
                      "separate archives in the GUI.")
        finally:
            src.close()
        self.save()
