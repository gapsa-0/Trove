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


def _cache_key(fid: int, sha256: str | None, rotate: int = 0) -> str:
    # Content-addressed when we have the hash: byte-identical duplicates then map
    # to the same file, so they can never show different frames. Fall back to fid
    # only for not-yet-hashed files. The rotation is part of the key so a photo
    # whose orientation is resolved later doesn't keep serving a sideways
    # thumbnail from before.
    base = sha256 if sha256 else f"fid{fid}"
    return f"{base}_v{THUMB_VER}" + (f"_r{rotate}" if rotate else "")


def _apply_rotation(im, rotate: int):
    """Turn a decoded image clockwise by 0/90/180/270 degrees.

    Uses the lossless transposes rather than ``rotate()`` so no resampling or
    off-by-one padding creeps into a quarter turn.
    """
    from PIL import Image
    transpose = {
        90: Image.Transpose.ROTATE_270,     # PIL names turns counter-clockwise
        180: Image.Transpose.ROTATE_180,
        270: Image.Transpose.ROTATE_90,
    }.get(rotate)
    return im.transpose(transpose) if transpose else im


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
FACE_THUMB_VER = 2


def _face_key(fid: int, sha256: str | None, box, rotate: int = 0) -> str:
    x, y, w, h = box
    base = sha256 if sha256 else f"fid{fid}"
    return (f"{base}_{x}_{y}_{w}_{h}_fv{FACE_THUMB_VER}"
            + (f"_r{rotate}" if rotate else ""))


def face_thumb_for(cache_dir: str, face_id: int, src: Path, box,
                   sha256: str | None = None, size: int = 200,
                   rotate: int = 0) -> Path | None:
    """A padded square crop around one face box, disk-cached. Content-addressed
    (sha + box) so the same face in byte-identical duplicates shares one crop.
    Read-only over the original; returns None if Pillow is unavailable.

    ``rotate`` is applied before cropping: boxes are stored in the frame the
    detector actually looked at, which for a sideways-stored photo is the turned
    one."""
    tp = Path(cache_dir) / "faces" / \
        f"{_face_key(face_id, sha256, box, rotate)}_{size}.jpg"
    if tp.exists():
        return tp
    pil = _try_pillow()
    if pil is None:
        return None
    Image, ImageOps = pil
    try:
        tp.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as im:
            im = _apply_rotation(ImageOps.exif_transpose(im), rotate).convert("RGB")
            W, H = im.size
            x, y, w, h = box
            cx, cy = x + w / 2, y + h / 2
            # Keep the crop square even when the face is close to an image
            # edge.  Clamping each edge independently made those crops
            # rectangular, leaving visible side gutters in square UI slots.
            side = max(1, min(round(max(w, h) * 1.6), W, H))
            l = max(0, min(round(cx - side / 2), W - side))
            t = max(0, min(round(cy - side / 2), H - side))
            r, b = l + side, t + side
            if r <= l or b <= t:
                return None
            crop = im.crop((l, t, r, b))
            resampling = getattr(Image, "Resampling", Image).LANCZOS
            crop = crop.resize((size, size), resampling)
            crop.save(tp, "JPEG", quality=82)
        return tp
    except Exception:
        return None


def thumb_for(cache_dir: str, fid: int, src: Path, size: int = 320,
              sha256: str | None = None, rotate: int = 0) -> Path | None:
    tp = Path(cache_dir) / "thumbs" / \
        f"{_cache_key(fid, sha256, rotate)}_{size}.jpg"
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
            im = _apply_rotation(ImageOps.exif_transpose(im), rotate)
            im.thumbnail((size, size))
            im.convert("RGB").save(tp, "JPEG", quality=80)
        return tp
    except Exception:
        return None


# Bump when the upright full-size rendering changes.
UPRIGHT_VER = 1


def upright_for(cache_dir: str, fid: int, src: Path, rotate: int,
                sha256: str | None = None) -> Path | None:
    """A full-size copy of a sideways-stored photo, turned upright.

    Only for the viewer, and only when ``rotate`` is non-zero — an untouched
    photo is always served as its own bytes. Re-encoding is the honest cost of
    never writing to the original: the file on disk stays exactly as it was.
    """
    if not rotate:
        return None
    tp = Path(cache_dir) / "upright" / \
        f"{_cache_key(fid, sha256, rotate)}_u{UPRIGHT_VER}.jpg"
    if tp.exists():
        return tp
    pil = _try_pillow()
    if pil is None:
        return None
    Image, ImageOps = pil
    try:
        tp.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as im:
            im = _apply_rotation(ImageOps.exif_transpose(im), rotate)
            im.convert("RGB").save(tp, "JPEG", quality=90)
        return tp
    except Exception:
        return None
