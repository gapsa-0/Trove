"""Optional image/video thumbnails for the grid.

If Pillow is available, generate and disk-cache a small JPEG per image; else
return None and the server falls back to serving the original (the browser
scales it). Video thumbnails are a single extracted frame via ffmpeg, if
present on the system. Read-only over originals.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..runtime import tool

_HEIF_REGISTERED = False

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".wmv", ".3gp", ".mkv", ".m4v", ".mpg", ".mpeg", ".webm"}

# Bump when the extraction logic changes (e.g. a different video seek). The
# cache key embeds it, so old thumbnails made by earlier logic are ignored and
# regenerated instead of being served forever — a fid-only key never could.
THUMB_VER = 1


def _cache_key(fid: int, sha256: str | None) -> str:
    # Content-addressed when we have the hash: byte-identical duplicates then map
    # to the same file, so they can never show different frames. Fall back to fid
    # only for not-yet-hashed files.
    return f"{sha256}_v{THUMB_VER}" if sha256 else f"fid{fid}_v{THUMB_VER}"


def _try_pillow():
    global _HEIF_REGISTERED
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    if not _HEIF_REGISTERED:
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except Exception:
            pass
        _HEIF_REGISTERED = True
    return Image, ImageOps


def _video_frame(tp: Path, src: Path, size: int, offset: str) -> bool:
    try:
        subprocess.run(
            [tool("ffmpeg"), "-y", "-ss", offset, "-i", str(src), "-frames:v", "1",
             "-vf", f"scale={size}:-1:force_original_aspect_ratio=decrease",
             "-q:v", "4", str(tp)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
        )
    except Exception:
        return False
    return tp.exists() and tp.stat().st_size > 0


def _thumb_video(tp: Path, src: Path, size: int) -> Path | None:
    tp.parent.mkdir(parents=True, exist_ok=True)
    # Try a frame a second in first (skips black opening frames on many
    # clips); very short videos have no frame there, so fall back to t=0.
    if _video_frame(tp, src, size, "00:00:01") or _video_frame(tp, src, size, "00:00:00"):
        return tp
    return None


# Bump when the face-crop framing changes so old crops are regenerated.
FACE_THUMB_VER = 1


def _face_key(fid: int, sha256: str | None, box) -> str:
    x, y, w, h = box
    base = sha256 if sha256 else f"fid{fid}"
    return f"{base}_{x}_{y}_{w}_{h}_fv{FACE_THUMB_VER}"


def face_thumb_for(cache_dir: str, face_id: int, src: Path, box,
                   sha256: str | None = None, size: int = 200) -> Path | None:
    """A padded square crop around one face box, disk-cached. Content-addressed
    (sha + box) so the same face in byte-identical duplicates shares one crop.
    Read-only over the original; returns None if Pillow is unavailable."""
    tp = Path(cache_dir) / "faces" / f"{_face_key(face_id, sha256, box)}_{size}.jpg"
    if tp.exists():
        return tp
    pil = _try_pillow()
    if pil is None:
        return None
    Image, ImageOps = pil
    try:
        tp.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            W, H = im.size
            x, y, w, h = box
            cx, cy = x + w / 2, y + h / 2
            side = max(w, h) * 1.6         # a little context around the face
            l = max(0, int(cx - side / 2)); t = max(0, int(cy - side / 2))
            r = min(W, int(cx + side / 2)); b = min(H, int(cy + side / 2))
            if r <= l or b <= t:
                return None
            crop = im.crop((l, t, r, b))
            crop.thumbnail((size, size))
            crop.save(tp, "JPEG", quality=82)
        return tp
    except Exception:
        return None


def thumb_for(cache_dir: str, fid: int, src: Path, size: int = 320,
              sha256: str | None = None) -> Path | None:
    tp = Path(cache_dir) / "thumbs" / f"{_cache_key(fid, sha256)}_{size}.jpg"
    if tp.exists():
        return tp
    if src.suffix.lower() in VIDEO_EXTS:
        return _thumb_video(tp, src, size)
    pil = _try_pillow()
    if pil is None:
        return None
    Image, ImageOps = pil
    try:
        tp.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((size, size))
            im.convert("RGB").save(tp, "JPEG", quality=80)
        return tp
    except Exception:
        return None
