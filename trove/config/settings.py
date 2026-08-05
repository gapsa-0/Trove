"""Configuration: source roots, data/cache locations, ignore rules, thresholds.

Mutable state lives in the current user's application-data directory. An optional
``config.json`` there overrides the defaults.
"""

# config.json persists most Config fields, so changing a dataclass default does
# nothing on an existing install: the persisted file on disk wins over the new
# default every time Config.load() runs. Retuning a field that is already live
# on an install means editing that install's config.json, not just this file.

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..paths import (
    app_data_dir,
    config_file,
    default_cache_dir,
    default_db_path,
    ensure_app_data_dirs,
    secrets_file,
)
from .archives import ArchiveRegistryMixin

# Project layout ------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Credentials this application no longer has any use for. Semantic search became
# local (trove/embeddings), so nothing can spend a Voyage key any
# more -- and a live credential left readable on disk for a feature that no
# longer exists is a privacy wart, not merely dead data.
_SUPERSEDED_SECRETS = ("voyage_api_key",)


def discard_superseded_secrets() -> None:
    """Remove retired credentials from ``secrets.json``, and the file with them.

    Idempotent and silent: this runs at every start, and a missing or unreadable
    secrets file simply means there is nothing to clean up.
    """
    path = secrets_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(data, dict) or not any(k in data for k in _SUPERSEDED_SECRETS):
        return
    for key in _SUPERSEDED_SECRETS:
        data.pop(key, None)
    try:
        if data:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
        else:
            path.unlink()
    except OSError:
        pass


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


@dataclass
class Config(ArchiveRegistryMixin):
    # ``roots``/``db_path``/``cache_dir`` remain the CLI's single shared catalog
    # (``trove scan``/``trove dedup``/... — a separate, unchanged tool). The GUI's
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
    # The DEFAULT pause state for an archive that has none of its own. Pause
    # itself is per-archive and lives on the archive's registry entry above
    # (``archive_pause``/``set_archive_pause``): a user who stops work on one
    # folder has said nothing about the next one they open, and a pause that
    # followed them across looked like the app had wedged. The GUI therefore
    # never writes these two; they remain as a config-file (or test-harness)
    # switch for bringing the app up with the background pipeline off.
    pipeline_paused: bool = False
    # Same, for the per-stage pause: display card ids ("scan", "dedup",
    # "detect", "places", "semantic" — see pipeline/stages.py's CARD_ORDER).
    # Independent of the whole-pipeline flag above and only meaningful while it
    # is off: a paused stage is skipped by the scheduler while its siblings
    # keep running.
    paused_stages: list[str] = field(default_factory=list)

    # Semantic Browse search, run locally by trove/embeddings. These
    # two are provenance, recorded on every row of semantic_embeddings; they are
    # not knobs. Changing them without changing the model would mislabel every
    # vector written afterwards.
    semantic_embedding_model: str = "siglip2-base-patch16-256"
    semantic_embedding_dimensions: int = 768
    # Two cuts decide which semantic matches are shown, because one absolute
    # number provably cannot. The local embedder's cosines are compressed AND
    # shift per query, so the two populations overlap: measured over 30 present
    # and 11 absent subjects on a 497-file archive, "a dog" (present) tops out
    # at 0.0916 while "the surface of mars" (absent) reaches 0.0948. Any single
    # threshold between them hides the dogs and shows the Mars lookalikes.
    #
    # The two cuts work because each binds on a different population. This one
    # binds only when a query's *best* score is low -- i.e. when the archive
    # holds nothing like it -- so it is what silences a query the archive
    # cannot answer, rather than a general noise floor. It gets safer as an
    # archive grows: the median best score rises with file count, so a fixed
    # floor binds less, not more. Retune with tools/dev/semantic_calibrate.py,
    # not by intuition.
    #
    # THIS FLOOR ASSUMES THE QUERY REACHES THE MODEL IN ENGLISH, which the GUI
    # guarantees by translating first (localEnglishTranslation, search.js). It
    # is a test on score *magnitude*, and magnitude is language-dependent:
    # SigLIP 2 is multilingual but trained 90% on English, so bare Spanish
    # nouns average 0.18 where English ones average 0.30 ("bosque" 0.130 vs
    # "forest" 0.348), and an article recovers almost none of it. With the
    # translator removed once, this value silenced "bosque", "montaña",
    # "nieve" and "calle" outright -- they returned nothing at all. Worse, the
    # Spanish populations overlap (present down to 0.119, absent up to 0.191),
    # so no value works for untranslated Spanish: that is a second, independent
    # reason the translator earns its 26 MB.
    #
    # BOTH VALUES BELOW ASSUME semantic_search_center_embeddings IS ON, which
    # roughly triples the score range. Uncentered they must be 0.07 / 0.75.
    semantic_search_min_similarity: float = 0.18
    # ... and this is the cut that shapes a query the archive *can* answer:
    # keep results within this fraction of the query's own best score, so the
    # bar travels with the query instead of assuming a fixed scale.
    #
    # Loose rather than tight because precision does not fall off a cliff: on a
    # hand-labelled subject every result stayed correct well past the cut, so a
    # tighter floor only discards true matches to buy a margin the scores never
    # needed. Measured over 30 present and 11 absent subjects, this pair returns
    # a median of 10 results for a present subject against 1 for an absent one,
    # where the old uncentered 0.05/0.80 returned 6 and 4 -- more of what you
    # want and less of what you don't, at the same time. The count scales
    # sub-linearly with archive size, so a large archive gets a fuller page
    # rather than a flood.
    semantic_search_relative_floor: float = 0.65
    # Modality-gap correction. Image and text embeddings occupy two clusters
    # separated by a near-constant offset, so raw cosines are squeezed into a
    # narrow band (0.046-0.146 across 41 measured subjects) and sit closer to
    # each other than to what they describe. Subtracting each modality's own
    # mean before the cosine collapses that offset: the same 41 subjects then
    # span 0.14-0.42, and "a dog" (0.0916 raw, below the absent subject "the
    # surface of mars" at 0.0948) moves to 0.3047 against 0.2216 -- the
    # inversion an uncentered score cannot express at any threshold.
    #
    # A wider scale needs a differently placed cut, so the two floors above are
    # tuned FOR this being on. Turning it off without restoring 0.07/0.75
    # leaves a bar far too high, and search will look broken.
    semantic_search_center_embeddings: bool = True

    # Reading the text inside documents.
    #
    # The size limit bounds ONE file, so a pathological input costs a skip
    # rather than the pass -- a 400 MB PDF of scanned plans should not hold the
    # stage while it is parsed. 64 MB comfortably clears real paperwork.
    documents_max_bytes: int = 67_108_864
    # How a document is cut into indexable passages. Characters, not tokens: a
    # .txt should not need a 17 MB tokenizer loaded to be read. The number is
    # sized against the *dense* end of the measured range rather than the
    # average, because dense is what a paperwork archive is full of -- see
    # trove/text/chunk.py, which carries the measurements. It is not a knob to
    # turn casually: changing either value changes every chunk boundary, which
    # means bumping TEXT_VERSION and re-reading the archive.
    documents_chunk_chars: int = 1200
    documents_chunk_overlap: int = 200

    # Reading text out of pictures.
    #
    # Detection and recognition deliberately run at different resolutions.
    # Detection is the half that runs on *every* image, and its cost is set by
    # input size -- measured on 4 cores: 1600px 1.51s, 1200px 0.87s, 960px
    # 0.62s, 736px 0.57s, and 512px no faster while missing boxes. So detection
    # sees a downscaled copy and recognition sees crops of the original, which
    # keeps small text readable: on a 2480x3508 scan the two-resolution path
    # returns byte-identical text to reading the whole thing at full size, and a
    # picture with no writing in it costs 0.59s instead of 1.51s.
    ocr_detect_side: int = 736
    # What a PDF page is rendered at before it is read. 200 puts 10pt body text
    # at about 28px tall, comfortably above what recognition needs, and costs a
    # quarter the memory of 300.
    ocr_render_dpi: int = 200
    # A page needs reading as pictures when it has almost no text layer AND is
    # mostly covered by one image. Both, because sparse text alone is not
    # evidence -- a title page, a section divider or a page holding one table
    # has almost no characters and nothing OCR could add, and on a long document
    # those are common enough that treating them as scans would be most of a
    # wasted run.
    ocr_text_layer_chars_per_page: int = 40
    ocr_min_image_cover: float = 0.5
    # One file may not hold the stage indefinitely: a 2,000-page scanned book is
    # an hour of OCR on its own. Beyond this it is skipped, with that reason.
    ocr_max_pages_per_file: int = 200

    # Searching documents has no tuning of its own. Its ranking is FTS5's BM25,
    # and MATCH is its own cut -- a document either contains the terms or is
    # absent -- so there is no relevance floor here to answer
    # ``semantic_search_min_similarity``.

    # Hashing
    fast_hash_sample_bytes: int = 65536  # head+tail sample for the cheap prefilter

    # Dedup (Phase 4)
    phash_hamming_threshold: int = 6

    # Places (geo clustering). A one-off snapshot at some random spot (a shop,
    # a stranger's house passed through once) shouldn't earn "place" status
    # just because a photo happened to have GPS there. This is a READ-time
    # filter, not a clustering-time one: place_clusters rows are never deleted
    # for falling short (places are durable, and incremental assignment
    # necessarily starts every new place at 1 member before it can ever grow),
    # they simply stop being *reported* by services/places.py's place_clusters()
    # once below this floor. Below-threshold members show as having no location
    # in the GUI — that's intended, not a bug ("orphan, no problem"). Named or
    # pinned clusters are exempt (same function): a user-named place, or one
    # just created via create_place() with 0-1 members, is intentional.
    place_min_media: int = 10

    # Drag-to-merge confirmation threshold. The clustering radius above is
    # 300 m, so a genuine "one location got split into adjacent clusters"
    # merge is almost always sub-kilometre -- this is the point past which a
    # proposed merge is spread out enough that the GUI should make the user
    # confirm before committing to it. It is NOT a limit: nothing is ever
    # refused for exceeding it, merge_place_clusters will still merge two
    # places on opposite sides of the country if asked. Like place_min_media,
    # config.json persists this field, so retuning it on an existing install
    # means editing config.json, not just this default.
    place_merge_warn_km: float = 20.0

    # Faces (Phase 6). Detection + embedding run locally; nothing leaves the
    # machine. Detection is InsightFace SCRFD (det_10g) from the buffalo_l pack,
    # fetched once into cache/models/insightface/; each face is aligned to the
    # 112x112 ArcFace template (5-point similarity transform) and embedded by
    # AdaFace ir101/WebFace12M, a 512-d vector run via onnxruntime from the
    # self-exported ONNX in cache/models/adaface/ (see faces/backend).
    # SCRFD replaced YuNet (fewer sky/wall false positives, better small-face
    # recall + landmarks) and stays. The embedder moved back to AdaFace from
    # ArcFace because AdaFace's feature norm doubles as the face-image-quality
    # score the Phase-1 gate needs (faces/fiqa.py) — one model, two signals.
    # Changing the embedder changes the vectors, so it requires a full re-extract
    # (see faces/migrate_adaface.py, which carries names and pins across it).
    faces_det_size: int = 640  # SCRFD detector input square (bigger = better
    # small-face recall, slower)
    faces_min_score: float = 0.50  # accept faces at/above this SCRFD confidence
    # (SCRFD's own floor is ~0.5; real frontal
    # faces score ~0.8+). Animal faces are handled
    # by the pet cross-check, not this threshold.
    faces_min_px: int = 50  # drop faces smaller than this (box side, px,
    # measured in ORIGINAL pixels). The Phase-1
    # base filter: below ~50px there is too little
    # detail for a trustworthy embedding, and such
    # faces are exactly the weak "bridge" vectors
    # that used to chain distinct people together.
    faces_max_side: int = 960  # standalone backend decode cap (the fused
    # detect stage decodes once at detect_max_side)
    faces_max_clipped_fraction: float = 0.18  # reject a box mostly outside the frame
    # Advisory quality metrics (focus/exposure) are still measured on the aligned
    # crop and stored per face for display/calibration, but no longer gate the live
    # path — SCRFD's confidence is the primary filter. Used by `trove faces --calibrate`.
    faces_min_focus: float = 35.0  # grayscale Laplacian variance (advisory)
    faces_max_extreme_fraction: float = 0.80  # near-black/near-white pixels (advisory)
    faces_quality_version: str = "opencv-laplacian-v1"

    # FIQA quality gate (Phase 1, faces/fiqa.py). Every embedded face gets a 0..1
    # score and is routed to a tier: HIGH may seed a cluster core, BORDERLINE may
    # only attach to a core someone else formed, LOW_QUALITY is excluded from
    # clustering altogether and hidden from the GUI. This is the gate that stops
    # blurry / extreme-profile / false-positive faces from bridging identities.
    #
    # The score is AdaFace's own feature norm, which the AdaFace paper establishes
    # as a proxy for image quality (it is what the model uses to set its adaptive
    # margin). Using it costs nothing: the embedder already computes it. The raw
    # norm is a model-specific magnitude, not a probability, so it is mapped to
    # 0..1 against the archive's own norm distribution — mean/std persisted in the
    # `fiqa_calibration` table, NOT recomputed per batch (per-batch statistics
    # would make a face's tier depend on when it was scanned).
    faces_fiqa_model: str = "adaface-norm-v1"
    # Squash half-width, in standard deviations: the score saturates at 0 below
    # mean - h*std and at 1 above mean + h*std. NOT the AdaFace paper's h=0.33 —
    # that value is tuned for a training-time margin over per-batch statistics,
    # and reusing it here squeezes the whole 0..1 range into +-0.9 norm units, so
    # measured on this archive 42% of faces pinned to exactly 0.0 and only 7%
    # landed between the tiers. h=2.0 spreads the score across the distribution
    # that actually exists (measured: mean 21.9, std 2.85 over 705 faces), giving
    # ~10% LOW_QUALITY / ~40% BORDERLINE / ~50% HIGH — a real borderline band for
    # pass 2 to work on, and a discard tier that is only the genuinely unusable.
    faces_fiqa_h: float = 2.0
    faces_fiqa_high: float = 0.55  # >= this is HIGH (core-eligible); ~the median
    # face, so about half the archive seeds cores
    faces_fiqa_low: float = 0.18  # < this is LOW_QUALITY (excluded from
    # clustering and hidden in the GUI); ~the
    # bottom decile. In between is BORDERLINE.
    faces_fiqa_calib_sample: int = 2000  # faces used to fix mean/std, once

    # Fused detection (Phase 6/9). People (SCRFD) and animals (YOLOX) are found in
    # ONE pass: each image is decoded a single time at this resolution and both
    # detectors run on that array, so ~150k photos are decoded once, not twice.
    detect_max_side: int = 1280  # long-side cap for the single shared decode

    # Video detection. Videos are detected from a handful of sampled keyframes
    # rather than decoded frame-by-frame (mirrors semantic.py's video indexing).
    detect_video_frames: int = 5  # keyframes sampled per video; 0 disables
    # video detection entirely (falls back to
    # today's images-only behaviour)
    detect_video_frame_px: int = 1280  # width frames are extracted at. LOAD-
    # BEARING, not cosmetic: detection boxes for
    # a video are stored in the *extracted
    # frame's* pixel coordinates, so a crop must
    # re-extract at this exact size later.
    # Changing it invalidates existing video
    # detections (their boxes no longer line up
    # with a freshly extracted frame).
    detect_video_same_face: float = 0.55  # cosine similarity above which two
    # faces found in different frames of the
    # SAME video are considered the same person
    # and collapsed to one row
    detect_video_same_animal: float = 0.80  # same, for animal DINOv2 embeddings
    # (same species is additionally required)

    # Pets. YOLOX detects animal regions locally; identities are grouped from a
    # DINOv2 re-ID embedding of each crop (cache/models/dinov2_pet/). The animal
    # boxes also cross-check the face pass: a face mostly inside an animal box is
    # dropped from People (the one non-human rule, applied inline in detect/).
    pets_min_score: float = 0.60
    pets_min_px: int = 48
    pets_max_side: int = 1280  # standalone backend decode cap (see detect_max_side)
    pets_species: list[str] = field(default_factory=lambda: ["cat", "dog", "bird", "horse"])
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
    faces_knn_k: int = 5  # stage-1 mutual-kNN neighbours per face
    faces_link_sim: float = 0.50  # stage-1 mutual-kNN similarity floor (cosine)
    faces_merge_sim: float = 0.40  # stage-2 avg-linkage merge (mean cosine sim)
    faces_centroid_merge_sim: float = 0.55  # stage-3 centroid-direction merge (cosine)
    faces_min_faces: int = 3  # min faces for a cluster to become a person

    # Core-expansion clustering (Phase 3). The stages above no longer run over the
    # whole population: they run over HIGH-quality faces only, building "cores"
    # that are pure by construction, and BORDERLINE faces are then attached to
    # those cores in a second pass. A borderline face can join a core but can
    # never create one and can never merge two — which is precisely the move that
    # cannot happen for a bridge face, so bridges stop mattering.
    #
    # faces_core_link_sim is deliberately much stricter than faces_link_sim: the
    # core pass wants small, unambiguous fragments, and stages 2-3 (average- and
    # centroid-linkage) are what put a person's fragments back together
    # afterwards, so strictness here costs recall only if those merges fail.
    faces_core_link_sim: float = 0.75  # pass-1 mutual-kNN floor within cores
    faces_border_assign_sim: float = 0.55  # pass-2: attach a borderline face to a
    # core at/above this cosine, else leave it as
    # noise. Lenient by design — the purity is
    # already guaranteed by who built the core.
    faces_border_votes: int = 3  # compare against a core's top-N members, not
    # just its centroid: a spread-out core's mean
    # understates similarity to its own members.

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

    # Semantic settings written by a pre-SigLIP install. config.json persists
    # every field, so a saved value shadows the dataclass default forever --
    # which would have the app writing 768-d SigLIP vectors while recording
    # `dimensions: 1024`, and filtering them at a threshold tuned for a
    # completely different similarity scale, so every search returned nothing.
    # Unlike the catalogue itself, config.json is not discarded on upgrade, so
    # these three have to be actively reset.
    _SUPERSEDED_SEMANTIC = (
        "semantic_embedding_model",
        "semantic_embedding_dimensions",
        "semantic_search_min_similarity",
        "semantic_search_relative_floor",
    )

    @classmethod
    def load(cls) -> Config:
        cfg = cls()
        path = config_file()
        if path.exists():
            data = json.loads(path.read_text())
            superseded = str(data.get("semantic_embedding_model", "")).startswith("voyage")
            for k, v in data.items():
                if superseded and k in cls._SUPERSEDED_SEMANTIC:
                    continue
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
            if superseded:
                cfg.save()
        return cfg

    def save(self) -> None:
        app_data_dir().mkdir(parents=True, exist_ok=True)
        config_file().write_text(json.dumps(asdict(self), indent=2))

    def ensure_dirs(self) -> None:
        ensure_app_data_dirs()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.cache_dir).mkdir(parents=True, exist_ok=True)
