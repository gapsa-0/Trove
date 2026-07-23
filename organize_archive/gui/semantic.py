"""Gemini Embedding 2 client and local embedding-index helpers.

The Gemini key is read only from ``GEMINI_API_KEY`` in the project-root .env.
Media bytes leave the machine when the automatic semantic-index job runs.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..db import database as db
from . import thumbs

_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent"
_IMAGE_MIMES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}
_OTHER_MIMES = {
    "mp4": "video/mp4", "mov": "video/quicktime",
    "mp3": "audio/mpeg", "wav": "audio/wav", "pdf": "application/pdf",
}
_RPM_LIMIT = 100
_TPM_LIMIT = 30_000
_RPD_LIMIT = 1_000
_MAX_INPUT_TOKENS = 8_192
INDEXER_VERSION = "2"


class GeminiEmbeddingError(RuntimeError):
    pass


def api_key_available() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY", "").strip())


def _api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise GeminiEmbeddingError("GEMINI_API_KEY is not set for this app process")
    return key


def _reserve_quota(db_path: str, cancel=None) -> int:
    """Atomically reserve one request at worst-case token cost.

    The embedding endpoint cannot tell us an input's multimodal token count in
    advance. Reserving 8,192 prevents a request from crossing the TPM ceiling;
    successful responses later shrink that reservation to actual usage.
    """
    while True:
        if cancel is not None and cancel.is_set():
            raise KeyboardInterrupt
        now = time.time()
        today = datetime.now(timezone.utc).date().isoformat()
        conn = db.connect(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            used_today = conn.execute(
                "SELECT COUNT(*) FROM semantic_api_usage WHERE usage_day=?", (today,)
            ).fetchone()[0]
            if used_today >= _RPD_LIMIT:
                conn.rollback()
                raise GeminiEmbeddingError(f"Gemini daily request limit reached ({_RPD_LIMIT} RPD)")
            recent = conn.execute(
                """SELECT requested_at, token_count FROM semantic_api_usage
                   WHERE requested_at>=? ORDER BY requested_at""", (now - 60,)
            ).fetchall()
            tokens = sum(r["token_count"] for r in recent)
            wait = 0.0
            if len(recent) >= _RPM_LIMIT:
                wait = max(wait, recent[0]["requested_at"] + 60 - now)
            if tokens + _MAX_INPUT_TOKENS > _TPM_LIMIT:
                released = 0
                for item in recent:
                    released += item["token_count"]
                    if tokens - released + _MAX_INPUT_TOKENS <= _TPM_LIMIT:
                        wait = max(wait, item["requested_at"] + 60 - now)
                        break
                else:
                    wait = max(wait, 60.0)
            if wait <= 0:
                cur = conn.execute(
                    "INSERT INTO semantic_api_usage(requested_at, usage_day, token_count) VALUES(?,?,?)",
                    (now, today, _MAX_INPUT_TOKENS),
                )
                conn.commit()
                return cur.lastrowid
            conn.rollback()
        finally:
            conn.close()
        # Keep cancellation responsive while waiting for a rolling quota window.
        deadline = time.monotonic() + max(0.1, wait)
        while time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                raise KeyboardInterrupt
            time.sleep(min(0.25, deadline - time.monotonic()))


def _finalize_quota(db_path: str, usage_id: int, body: dict) -> None:
    usage = body.get("usageMetadata") or body.get("usage_metadata") or {}
    tokens = usage.get("totalTokenCount") or usage.get("promptTokenCount")
    if not isinstance(tokens, int) or tokens < 1:
        return  # Keep the conservative reservation when usage is unavailable.
    conn = db.connect(db_path)
    try:
        conn.execute("UPDATE semantic_api_usage SET token_count=? WHERE id=?", (tokens, usage_id))
        conn.commit()
    finally:
        conn.close()


def _embed(cfg, part: dict, db_path: str, cancel=None) -> list[float]:
    usage_id = _reserve_quota(db_path, cancel)
    payload = {
        "content": {"parts": [part]},
        "output_dimensionality": cfg.semantic_embedding_dimensions,
    }
    request = Request(
        _ENDPOINT.format(model=cfg.semantic_embedding_model),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": _api_key()},
        method="POST",
    )
    try:
        with urlopen(request, timeout=60) as response:
            body = json.loads(response.read())
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise GeminiEmbeddingError(f"Gemini returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise GeminiEmbeddingError(f"Could not reach Gemini: {exc.reason}") from exc
    except TimeoutError as exc:
        raise GeminiEmbeddingError("Gemini embedding request timed out") from exc
    _finalize_quota(db_path, usage_id, body)
    try:
        # REST embedContent returns one ``embedding`` object. The official SDK
        # exposes that same response as an ``embeddings`` list, so accept both
        # shapes to keep this stdlib REST client aligned with the API.
        embedding = body.get("embedding")
        if embedding is None:
            embedding = body["embeddings"][0]
        values = embedding["values"]
    except (KeyError, IndexError, TypeError) as exc:
        raise GeminiEmbeddingError("Gemini returned no embedding values") from exc
    if len(values) != cfg.semantic_embedding_dimensions:
        raise GeminiEmbeddingError(
            f"Gemini returned {len(values)} dimensions, expected {cfg.semantic_embedding_dimensions}"
        )
    return [float(v) for v in values]


def embed_query(cfg, query: str, db_path: str) -> list[float]:
    return _embed(cfg, {"text": query}, db_path)


def media_part(cfg, path: Path, ext: str, media_type: str) -> tuple[dict | None, str | None, str | None]:
    """Return an inline Gemini part, input kind, and a non-fatal skip reason."""
    ext = (ext or path.suffix.lstrip(".")).lower()
    source, mime, kind = path, _IMAGE_MIMES.get(ext), "image"
    if media_type == "image" and mime is None:
        # Gemini accepts JPEG/PNG. Existing thumbnail generation converts the
        # archive's HEIC/RAW/WebP/etc. into a representative local JPEG.
        cache_id = int.from_bytes(hashlib.sha256(str(path).encode()).digest()[:8], "big")
        source = thumbs.thumb_for(cfg.cache_dir, cache_id, path, size=1024)
        mime, kind = "image/jpeg", "thumbnail"
    elif media_type != "image":
        mime = _OTHER_MIMES.get(ext)
        kind = media_type
    if source is None or mime is None:
        return None, None, f"unsupported {media_type} format: .{ext or 'unknown'}"
    try:
        size = source.stat().st_size
    except OSError as exc:
        return None, None, f"cannot read media: {exc}"
    if size > cfg.semantic_inline_max_bytes:
        # A full-resolution JPEG/PNG can still use its locally generated JPEG.
        if media_type == "image" and source == path:
            cache_id = int.from_bytes(hashlib.sha256(str(path).encode()).digest()[:8], "big")
            source = thumbs.thumb_for(cfg.cache_dir, cache_id, path, size=1024)
            if source is not None and source.stat().st_size <= cfg.semantic_inline_max_bytes:
                mime, kind = "image/jpeg", "thumbnail"
            else:
                return None, None, f"media exceeds inline limit ({size / 1024 / 1024:.1f} MB)"
        else:
            return None, None, f"media exceeds inline limit ({size / 1024 / 1024:.1f} MB)"
    try:
        data = base64.b64encode(source.read_bytes()).decode("ascii")
    except OSError as exc:
        return None, None, f"cannot read media: {exc}"
    return {"inline_data": {"mime_type": mime, "data": data}}, kind, None


def embed_media(cfg, path: Path, ext: str, media_type: str, db_path: str,
                cancel=None) -> tuple[list[float] | None, str | None, str | None]:
    part, kind, skip_reason = media_part(cfg, path, ext, media_type)
    if skip_reason:
        return None, kind, skip_reason
    return _embed(cfg, part, db_path, cancel), kind, None


def pending_rows(conn, root_id: int | None, force: bool = False):
    where = ["f.present=1", "f.hidden=0", "f.media_type IN ('image','video','audio','document')"]
    params: list = []
    if root_id is not None:
        where.append("f.root_id=?")
        params.append(root_id)
    if not force:
        # A code/indexer revision retries previous errors once. Successful and
        # deliberately skipped files stay put, preventing an invalid input from
        # waking the automatic scheduler forever.
        where.append("(e.file_id IS NULL OR e.source_sha256 IS NOT f.sha256 "
                     "OR COALESCE(e.indexer_version, '') != ?)")
        params.append(INDEXER_VERSION)
    return conn.execute(
        f"""SELECT f.id, f.rel_path, f.ext, f.media_type, f.sha256, r.path AS root_path
             FROM files f JOIN roots r ON r.id=f.root_id
             LEFT JOIN semantic_embeddings e ON e.file_id=f.id
             WHERE {' AND '.join(where)} ORDER BY f.id""",
        params,
    ).fetchall()


def save_outcome(conn, cfg, row, values, kind: str | None, error: str | None) -> None:
    status = "indexed" if values is not None else ("skipped" if error and error.startswith(("unsupported", "media exceeds")) else "error")
    import struct
    blob = struct.pack(f"<{len(values)}f", *values) if values is not None else None
    conn.execute(
        """INSERT INTO semantic_embeddings
               (file_id, source_sha256, model, dimensions, embedding, status, input_kind, error, indexer_version, indexed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(file_id) DO UPDATE SET
               source_sha256=excluded.source_sha256, model=excluded.model,
               dimensions=excluded.dimensions, embedding=excluded.embedding,
               status=excluded.status, input_kind=excluded.input_kind,
               error=excluded.error, indexer_version=excluded.indexer_version,
               indexed_at=excluded.indexed_at""",
        (row["id"], row["sha256"] or "", cfg.semantic_embedding_model,
         cfg.semantic_embedding_dimensions, blob, status, kind, error,
         INDEXER_VERSION, db.now_iso()),
    )
