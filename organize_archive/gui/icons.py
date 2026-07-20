"""Generate the PWA app icon (disk-cached). Uses Pillow when available; falls
back to a tiny embedded PNG so the manifest always resolves."""

from __future__ import annotations

import base64
from pathlib import Path

# 1x1 dark PNG, used only if Pillow is unavailable.
_FALLBACK = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def app_icon(cache_dir: str, size: int) -> bytes:
    out = Path(cache_dir) / "icons" / f"icon-{size}.png"
    if out.exists():
        return out.read_bytes()
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return _FALLBACK

    bg = (20, 22, 26, 255)
    accent = (91, 157, 255, 255)
    white = (231, 233, 238, 255)

    im = Image.new("RGBA", (size, size), bg)
    d = ImageDraw.Draw(im)
    m = size // 8
    # rounded accent panel
    d.rounded_rectangle([m, m, size - m, size - m], radius=size // 8, fill=accent)
    # a simple "photo": sun + mountains, in the panel
    inner = size // 5
    left, top, right, bot = m + inner, m + inner, size - m - inner, size - m - inner
    d.ellipse([right - (right - left) // 3, top,
               right, top + (right - left) // 3], fill=white)  # sun
    d.polygon([(left, bot), (left + (right - left) * 0.4, top + (bot - top) * 0.45),
               (left + (right - left) * 0.65, bot)], fill=white)              # peak 1
    d.polygon([(left + (right - left) * 0.45, bot),
               (left + (right - left) * 0.75, top + (bot - top) * 0.6),
               (right, bot)], fill=white)                                     # peak 2

    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out, "PNG")
    return out.read_bytes()
