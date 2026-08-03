"""Semantic-index orchestration over the local SigLIP 2 embedder.

This layer decides *what* gets embedded and how the result is recorded; the
model itself lives in ``organize_archive/embeddings/backend.py``. It used to
call Voyage's multimodal API, which made semantic search the one feature that
sent photos and search queries off this machine. Nothing here reaches the
network any more except the one-time model download.

The archive is never read at full resolution: an image is embedded through the
same 1024px cached thumbnail the GUI already renders (so HEIC/RAW decode and the
archive's ``rotate_deg`` are handled once, and retrieval sees the photo the way
up the user sees it), and a video through a handful of frames sampled across the
clip. A video's frames are embedded individually and averaged into one vector,
which keeps a clip comparable to a photo under the same cosine threshold.

Vectors live in the ``semantic_embeddings`` table. ``INDEXER_VERSION`` is the
identity of that vector space: change the model or its preprocessing, bump it,
and every archive re-indexes itself instead of silently comparing vectors that
were never in the same space.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, cast

from .. import thumbnails
from ..config import Config
from ..db import database as db
from ..embeddings import backend as eb

logger = logging.getLogger(__name__)

# Bumped whenever the vector space changes. Mirrors embeddings.EMBEDDER_VERSION;
# kept as its own constant because pending_rows/work_counts compare it per row.
INDEXER_VERSION = eb.EMBEDDER_VERSION

# Frames sampled per video, and where in the clip: evenly spread but pulled in
# from the very ends, where a title card or a black open/close frame sits more
# often than a moment that actually represents the video.
_VIDEO_FRAME_SIZE = 1024
_VIDEO_FRAME_FRACTIONS = (0.15, 0.5, 0.85)
_IMAGE_THUMB_SIZE = 1024

# One backend for the whole process. The vision tower is 372 MB and takes a
# couple of seconds to load, so rebuilding it per job (or per search) would be
# pure waste; both towers inside it are still loaded lazily and independently,
# so an indexing run never pays for the text tower and a search never pays for
# the vision tower.
_backend: eb.SiglipBackend | None = None
_backend_lock = threading.Lock()


def available() -> bool:
    """True when the embedding dependencies are importable.

    Weights are *not* part of this: the indexing stage is what downloads them
    (see ``embeddings.backend.available``).
    """
    return eb.available()


def models_ready(cfg: Config) -> bool:
    """True when the indexing half (the vision tower) is already downloaded."""
    return eb.vision_ready(cfg.cache_dir)


def backend(cfg: Config) -> eb.SiglipBackend:
    """The process-wide SigLIP backend, created on first use and reused after.

    Takes no progress callback: whoever constructs the singleton would otherwise
    decide, for the rest of the process, where every later download reports to.
    Pass ``log`` to ``load_vision``/``load_text`` instead — those run per caller.
    """
    global _backend
    if _backend is None:
        with _backend_lock:
            if _backend is None:
                _backend = eb.SiglipBackend(cfg.cache_dir)
    return _backend


def warm_text_model(cfg: Config) -> None:
    """Load the text tower now, so the first search does not wait for it.

    A warm-up, never a download: if the weights are not on disk yet this returns
    immediately and the first search fetches them, inside a request that can
    report and fail visibly. A background thread at server start has nowhere to
    show a 317 MB download and nowhere to report its failure, and silently using
    someone's connection for it is not the startup behaviour to ship.

    Best-effort otherwise: any failure here must stay invisible, because the next
    search simply loads the tower again, or reports the real error.
    """
    if not eb.text_ready(cfg.cache_dir):
        return
    try:
        backend(cfg).load_text()
    except Exception:
        # Invisible to the caller by design (see docstring); recorded here so
        # the failure isn't lost outright if warmup is what actually broke.
        logger.debug("text tower warmup failed", exc_info=True)


def embed_queries(cfg: Config, queries: list[str]) -> list[list[float]]:
    """Embed related search formulations together in one forward pass."""
    vectors = backend(cfg).embed_texts(queries)
    return [[float(v) for v in vector] for vector in vectors]


def _cache_id(path: Path) -> int:
    return int.from_bytes(hashlib.sha256(str(path).encode()).digest()[:8], "big")


def _format_offset(secs: float) -> str:
    secs = max(0.0, secs)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def _video_frame_offsets(
    duration_s: Any,  # sqlite3 REAL column: None, an int/float, or (rarely) unparsable text
) -> list[str]:
    """ffmpeg ``-ss`` offsets for a handful of frames spread across the clip.

    Falls back to one fixed early offset when the duration is unknown (enrich
    hasn't run yet, or ffprobe couldn't read this container) rather than
    refusing to index the video at all.
    """
    try:
        duration_s = float(duration_s)
    except (TypeError, ValueError):
        duration_s = 0.0
    if duration_s <= 0:
        return ["00:00:01"]
    offsets, seen = [], set()
    for frac in _VIDEO_FRAME_FRACTIONS:
        text = _format_offset(min(duration_s * frac, max(0.0, duration_s - 0.05)))
        if text not in seen:
            seen.add(text)
            offsets.append(text)
    return offsets


def _video_frames_part(
    path: Path,
    cache_dir: str,
    rotate: int,
    duration_s: Any,  # see _video_frame_offsets: passed straight through to it
) -> tuple[list[Path] | None, str | None, str | None]:
    """A handful of frames sampled across the clip, instead of the whole video.

    Each is embedded separately and the unit vectors averaged, so a clip lands
    in the same space as a photo rather than being represented by whichever
    single frame happened to be first.
    """
    frames = thumbnails.video_frames_for(
        cache_dir,
        _cache_id(path),
        path,
        _video_frame_offsets(duration_s),
        size=_VIDEO_FRAME_SIZE,
        rotate=rotate,
    )
    if not frames:
        # "unsupported" prefix matches the other permanent-skip reasons below
        # (save_outcome/_is_permanent_skip): no ffmpeg, or a container it
        # can't read, is a clean permanent skip, not a retryable error.
        return (
            None,
            None,
            (
                "unsupported video: could not extract any frames "
                "(ffmpeg missing or unreadable video)"
            ),
        )
    return frames, "video_frames", None


def media_part(
    cfg: Config,
    path: Path,
    ext: str,
    media_type: str,
    cache_dir: str,
    rotate: int = 0,
    duration_s: Any = None,  # see _video_frame_offsets: passed straight through to it
) -> tuple[list[Path] | None, str | None, str | None]:
    """The image files to embed for one archive item, or a non-fatal skip reason.

    Returns cached JPEGs, never the original: one 1024px thumbnail for a photo,
    several sampled frames for a video. ``cache_dir`` is the calling archive's
    own cache — these are content derived from that archive and are never shared
    with another one. ``rotate`` renders a sideways-stored photo the way up the
    app shows it, so retrieval sees what the user sees. ``duration_s`` (from
    ``media_meta``, already populated by enrich for every video) spaces a
    video's sampled frames across its length; unused for other media types.
    """
    ext = (ext or path.suffix.lstrip(".")).lower()
    if media_type == "video":
        return _video_frames_part(path, cache_dir, rotate, duration_s)
    if media_type == "image":
        # The thumbnailer is the only decoder involved, so whatever it can open
        # is exactly what can be indexed: HEIC, RAW and friends included. If it
        # returns nothing, no amount of retrying will help.
        thumb = thumbnails.thumb_for(
            cache_dir, _cache_id(path), path, size=_IMAGE_THUMB_SIZE, rotate=rotate
        )
        if thumb is None:
            return None, None, f"unsupported image format: .{ext or 'unknown'}"
        return [thumb], "thumbnail", None
    if media_type == "audio":
        return None, None, "unsupported audio format (the search model has no audio tower)"
    return (
        None,
        None,
        ("unsupported document format (only visual media is indexed, not PDFs directly)"),
    )


def embed_part(cfg: Config, part: list[Path], kind: str | None) -> list[float] | None:
    """Embed one prepared item: a photo's thumbnail, or a video's frames.

    Returns None only when nothing in ``part`` could be decoded, which the
    caller records as a permanent skip.
    """
    model = backend(cfg)
    if kind == "video_frames":
        vector = model.embed_frames_mean(part)
    else:
        vectors = model.embed_images(part)
        vector = vectors[0] if len(vectors) else None
    return None if vector is None else [float(v) for v in vector]


def embed_media(
    cfg: Config,
    path: Path,
    ext: str,
    media_type: str,
    cache_dir: str,
    cancel: threading.Event | None = None,
    rotate: int = 0,
    duration_s: Any = None,  # see _video_frame_offsets: passed straight through to it
) -> tuple[list[float] | None, str | None, str | None]:
    """Prepare and embed one archive item end to end: (vector, input_kind,
    skip_reason). The vector is None whenever indexing didn't happen, with
    ``skip_reason`` explaining why (unsupported format, no frame decoded);
    raises ``KeyboardInterrupt`` if ``cancel`` is already set."""
    if cancel is not None and cancel.is_set():
        raise KeyboardInterrupt
    part, kind, skip_reason = media_part(cfg, path, ext, media_type, cache_dir, rotate, duration_s)
    if skip_reason:
        return None, kind, skip_reason
    # media_part's invariant: part is None iff skip_reason is set, just checked above.
    values = embed_part(cfg, cast(list[Path], part), kind)
    if values is None:
        return None, kind, f"unsupported {media_type}: no frame could be decoded"
    return values, kind, None


def pending_rows(
    conn: sqlite3.Connection, root_id: int | None, force: bool = False
) -> list[sqlite3.Row]:
    """Present, non-hidden files still needing (re-)indexing -- unindexed, with
    changed bytes, or stamped with a stale ``INDEXER_VERSION`` -- or, with
    ``force``, every eligible file regardless of what's already indexed."""
    where = ["f.present=1", "f.hidden=0", "f.media_type IN ('image','video','audio','document')"]
    params: list[Any] = []
    if root_id is not None:
        where.append("f.root_id=?")
        params.append(root_id)
    if not force:
        where.append(
            "(e.file_id IS NULL OR e.source_sha256 IS NOT f.sha256 "
            "OR COALESCE(e.indexer_version, '') != ?)"
        )
        params.append(INDEXER_VERSION)
    # rotate_deg: index the photo the way up the app shows it. Detection runs
    # alongside this stage rather than before it, so a photo indexed first is
    # embedded as stored; it is re-indexed only if its bytes change. duration_s
    # (populated by enrich for every video) spaces a video's sampled frames
    # across its length with no extra ffprobe call needed here.
    return conn.execute(
        f"""SELECT f.id, f.rel_path, f.ext, f.media_type, f.sha256, r.path AS root_path,
                   COALESCE(o.rotate_deg, 0) AS rotate_deg, m.duration_s
             FROM files f JOIN roots r ON r.id=f.root_id
             LEFT JOIN semantic_embeddings e ON e.file_id=f.id
             LEFT JOIN orientation o ON o.file_id=f.id
             LEFT JOIN media_meta m ON m.file_id=f.id
             WHERE {" AND ".join(where)} ORDER BY f.id""",
        params,
    ).fetchall()


def work_counts(
    conn: sqlite3.Connection, root_id: int | None, force: bool = False
) -> tuple[int, int]:
    """(total eligible files, how many are already indexed at the current
    ``INDEXER_VERSION``) for a progress readout. With ``force``, completed is
    always 0 since every eligible file is about to be re-indexed."""
    where = ["f.present=1", "f.hidden=0", "f.media_type IN ('image','video','audio','document')"]
    params: list[Any] = []
    if root_id is not None:
        where.append("f.root_id=?")
        params.append(root_id)
    total = conn.execute(
        f"SELECT COUNT(*) FROM files f WHERE {' AND '.join(where)}", params
    ).fetchone()[0]
    if force:
        return total, 0
    completed = conn.execute(
        f"""SELECT COUNT(*) FROM files f JOIN semantic_embeddings e ON e.file_id=f.id
            WHERE {" AND ".join(where)} AND e.source_sha256 IS f.sha256
              AND COALESCE(e.indexer_version, '') = ?""",
        (*params, INDEXER_VERSION),
    ).fetchone()[0]
    return total, completed


def _is_permanent_skip(error: str | None) -> bool:
    """An outcome nothing should ever promise to retry: the input itself is the
    problem (an unsupported or undecodable format), not a transient failure.
    Re-reading identical bytes would only reproduce the same result.
    """
    if not error:
        return False
    return error.startswith(("unsupported", "media exceeds"))


def save_outcome(
    conn: sqlite3.Connection,
    cfg: Config,
    row: sqlite3.Row,
    values: list[float] | None,
    kind: str | None,
    error: str | None,
) -> None:
    """Upsert one file's semantic_embeddings row from an ``embed_media`` result,
    deriving ``status`` ('indexed' / 'skipped' / 'error') from whether a vector
    was produced and, if not, whether the failure is permanent. Does not
    commit; the caller controls the transaction."""
    status = (
        "indexed" if values is not None else ("skipped" if _is_permanent_skip(error) else "error")
    )
    import struct

    blob = struct.pack(f"<{len(values)}f", *values) if values is not None else None
    conn.execute(
        """INSERT INTO semantic_embeddings
               (file_id, source_sha256, model, dimensions, embedding, status, input_kind, error, indexer_version, indexed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(file_id) DO UPDATE SET source_sha256=excluded.source_sha256,
               model=excluded.model, dimensions=excluded.dimensions, embedding=excluded.embedding,
               status=excluded.status, input_kind=excluded.input_kind, error=excluded.error,
               indexer_version=excluded.indexer_version, indexed_at=excluded.indexed_at""",
        (
            row["id"],
            row["sha256"] or "",
            cfg.semantic_embedding_model,
            cfg.semantic_embedding_dimensions,
            blob,
            status,
            kind,
            error,
            INDEXER_VERSION,
            db.now_iso(),
        ),
    )
