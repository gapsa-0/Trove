"""Voyage Multimodal client and local semantic-index helpers.

The archive index uses Voyage's multimodal endpoint for image and MP4 video
retrieval.  Vectors remain in SQLite; only source media sent for indexing and
the user's search query leave the machine.  Voyage's Batch API does not yet
support its multimodal model, so this module uses the endpoint's regular
multi-input requests instead.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..db import database as db
from . import thumbs

_ENDPOINT = "https://api.voyageai.com/v1/multimodalembeddings"
_IMAGE_MIMES = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "webp": "image/webp", "gif": "image/gif",
}
_VIDEO_MIMES = {"mp4": "video/mp4"}
_MAX_INPUTS = 1_000
INDEXER_VERSION = "voyage-mm-3.5-2"


class VoyageEmbeddingError(RuntimeError):
    pass


def api_key_available() -> bool:
    return bool(os.environ.get("VOYAGE_API_KEY", "").strip())


def _api_key() -> str:
    key = os.environ.get("VOYAGE_API_KEY", "").strip()
    if not key:
        raise VoyageEmbeddingError("VOYAGE_API_KEY is not set for this app process")
    return key


def _embed(cfg, inputs: list[dict], *, input_type: str) -> list[list[float]]:
    if not inputs:
        return []
    if len(inputs) > _MAX_INPUTS:
        raise ValueError(f"Voyage accepts at most {_MAX_INPUTS} inputs per request")
    payload = {
        "inputs": inputs,
        "model": cfg.semantic_embedding_model,
        "input_type": input_type,
        "truncation": True,
    }
    request = Request(
        _ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {_api_key()}"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=180) as response:
            body = json.loads(response.read())
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise VoyageEmbeddingError(f"Voyage returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise VoyageEmbeddingError(f"Could not reach Voyage: {exc.reason}") from exc
    except TimeoutError as exc:
        raise VoyageEmbeddingError("Voyage embedding request timed out") from exc
    try:
        data = body["data"]
        values = [item["embedding"] for item in sorted(data, key=lambda item: item["index"])]
    except (KeyError, TypeError) as exc:
        raise VoyageEmbeddingError("Voyage returned no embedding values") from exc
    if len(values) != len(inputs):
        raise VoyageEmbeddingError(f"Voyage returned {len(values)} embeddings for {len(inputs)} inputs")
    for vector in values:
        if len(vector) != cfg.semantic_embedding_dimensions:
            raise VoyageEmbeddingError(
                f"Voyage returned {len(vector)} dimensions, expected {cfg.semantic_embedding_dimensions}")
    return [[float(v) for v in vector] for vector in values]


def embed_query(cfg, query: str, _db_path: str) -> list[float]:
    return embed_queries(cfg, [query], _db_path)[0]


def embed_queries(cfg, queries: list[str], _db_path: str) -> list[list[float]]:
    """Embed related search formulations together in one Voyage request."""
    return _embed(cfg, [
        {"content": [{"type": "text", "text": query}]} for query in queries
    ], input_type="query")


def embed_parts(cfg, parts: list[dict]) -> list[list[float]]:
    """Embed prepared archive inputs together using Voyage's multi-input API."""
    return _embed(cfg, parts, input_type="document")


def media_part(cfg, path: Path, ext: str, media_type: str,
               cache_dir: str) -> tuple[dict | None, str | None, str | None]:
    """Create one Voyage multimodal input, or a non-fatal skip reason.

    ``cache_dir`` is the calling archive's own cache — semantic indexing
    thumbnails are content derived from that archive and never shared with
    another one.
    """
    ext = (ext or path.suffix.lstrip(".")).lower()
    source, mime, kind = path, _IMAGE_MIMES.get(ext), "image"
    content_type = "image_base64"
    if media_type == "image":
        # A 1024px JPEG keeps every request small enough to batch efficiently,
        # avoids sending private full-resolution originals, and remains ample
        # for archive-level visual retrieval. It also converts HEIC/RAW/etc.
        # to a format Voyage accepts.
        cache_id = int.from_bytes(hashlib.sha256(str(path).encode()).digest()[:8], "big")
        thumb = thumbs.thumb_for(cache_dir, cache_id, path, size=1024)
        if thumb is not None:
            source, mime, kind = thumb, "image/jpeg", "thumbnail"
        elif mime is None:
            return None, None, f"unsupported image format: .{ext or 'unknown'}"
    elif media_type == "video":
        mime, kind, content_type = _VIDEO_MIMES.get(ext), "video", "video_base64"
    elif media_type == "audio":
        return None, None, "unsupported audio format (Voyage multimodal has no audio input)"
    else:
        return None, None, "unsupported document format (Voyage indexes visual document images, not PDFs directly)"
    if source is None or mime is None:
        return None, None, f"unsupported {media_type} format: .{ext or 'unknown'}"
    try:
        size = source.stat().st_size
    except OSError as exc:
        return None, None, f"cannot read media: {exc}"
    if size > cfg.semantic_inline_max_bytes:
        # A large photo can use a local JPEG thumbnail; video must be skipped.
        if media_type == "image" and source == path:
            cache_id = int.from_bytes(hashlib.sha256(str(path).encode()).digest()[:8], "big")
            source = thumbs.thumb_for(cache_dir, cache_id, path, size=1024)
            if source is not None and source.stat().st_size <= cfg.semantic_inline_max_bytes:
                mime, kind, content_type = "image/jpeg", "thumbnail", "image_base64"
            else:
                return None, None, f"media exceeds inline limit ({size / 1024 / 1024:.1f} MB)"
        else:
            return None, None, f"media exceeds inline limit ({size / 1024 / 1024:.1f} MB)"
    try:
        data = base64.b64encode(source.read_bytes()).decode("ascii")
    except OSError as exc:
        return None, None, f"cannot read media: {exc}"
    return {"content": [{content_type: f"data:{mime};base64,{data}", "type": content_type}]}, kind, None


def embed_media(cfg, path: Path, ext: str, media_type: str, cache_dir: str,
                cancel=None) -> tuple[list[float] | None, str | None, str | None]:
    if cancel is not None and cancel.is_set():
        raise KeyboardInterrupt
    part, kind, skip_reason = media_part(cfg, path, ext, media_type, cache_dir)
    if skip_reason:
        return None, kind, skip_reason
    return _embed(cfg, [part], input_type="document")[0], kind, None


def pending_rows(conn, root_id: int | None, force: bool = False):
    where = ["f.present=1", "f.hidden=0", "f.media_type IN ('image','video','audio','document')"]
    params: list = []
    if root_id is not None:
        where.append("f.root_id=?")
        params.append(root_id)
    if not force:
        where.append("(e.file_id IS NULL OR e.source_sha256 IS NOT f.sha256 "
                     "OR COALESCE(e.indexer_version, '') != ?)")
        params.append(INDEXER_VERSION)
    return conn.execute(
        f"""SELECT f.id, f.rel_path, f.ext, f.media_type, f.sha256, r.path AS root_path
             FROM files f JOIN roots r ON r.id=f.root_id
             LEFT JOIN semantic_embeddings e ON e.file_id=f.id
             WHERE {' AND '.join(where)} ORDER BY f.id""", params).fetchall()


def work_counts(conn, root_id: int | None, force: bool = False) -> tuple[int, int]:
    where = ["f.present=1", "f.hidden=0", "f.media_type IN ('image','video','audio','document')"]
    params: list = []
    if root_id is not None:
        where.append("f.root_id=?")
        params.append(root_id)
    total = conn.execute(f"SELECT COUNT(*) FROM files f WHERE {' AND '.join(where)}", params).fetchone()[0]
    if force:
        return total, 0
    completed = conn.execute(
        f"""SELECT COUNT(*) FROM files f JOIN semantic_embeddings e ON e.file_id=f.id
            WHERE {' AND '.join(where)} AND e.source_sha256 IS f.sha256
              AND COALESCE(e.indexer_version, '') = ?""", (*params, INDEXER_VERSION)).fetchone()[0]
    return total, completed


def save_outcome(conn, cfg, row, values, kind: str | None, error: str | None) -> None:
    status = "indexed" if values is not None else ("skipped" if error and error.startswith(("unsupported", "media exceeds")) else "error")
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
        (row["id"], row["sha256"] or "", cfg.semantic_embedding_model,
         cfg.semantic_embedding_dimensions, blob, status, kind, error,
         INDEXER_VERSION, db.now_iso()))
