"""Optional image thumbnails for the grid.

If Pillow is available, generate and disk-cache a small JPEG per image; else
return None and the server falls back to serving the original (the browser
scales it). Read-only over originals.
"""

from __future__ import annotations

from pathlib import Path

_HEIF_REGISTERED = False


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


def thumb_for(cache_dir: str, fid: int, src: Path, size: int = 320) -> Path | None:
    tp = Path(cache_dir) / "thumbs" / f"{fid}_{size}.jpg"
    if tp.exists():
        return tp
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
