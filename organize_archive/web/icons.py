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

    im = _render(Image, ImageDraw, size)
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out, "PNG")
    return out.read_bytes()


# The Trove mark: a cut gem on a rounded, vertically graded tile. The SAME
# geometry is drawn as inline SVG in web/index.html (the `#trove-mark` symbol)
# for the picker bar and the sidebar -- both are authored in this 28-unit
# coordinate space, so a change to one must be mirrored in the other.
_TILE = 28.0
_TOP = (74, 163, 255)  # gradient stops, matching #4aa3ff -> #0062e0
_BOTTOM = (0, 98, 224)
_FACET = (10, 111, 224, 255)  # #0a6fe0, the facet lines drawn over the gem
_GEM = [(10.5, 8), (17.5, 8), (22, 13.5), (14, 22), (6, 13.5)]
_FACET_LINES = [
    [(6, 13.5), (22, 13.5)],
    [(10.5, 8), (12, 13.5), (14, 22)],
    [(17.5, 8), (16, 13.5), (14, 22)],
]


def _render(Image, ImageDraw, size: int):
    """Draw the mark at ``size`` px, supersampled 4x so the gem's diagonals and
    the tile's corner radius stay clean at the 32px favicon end of the range."""
    ss = 4
    px = size * ss
    scale = px / _TILE

    def pt(p):
        return (p[0] * scale, p[1] * scale)

    # Vertical gradient, then clipped to the rounded tile by using that shape as
    # the alpha mask (Pillow has no gradient fill for a rounded rectangle).
    grad = Image.new("RGB", (1, px))
    for y in range(px):
        t = y / max(1, px - 1)
        grad.putpixel(
            (0, y), tuple(round(a + (b - a) * t) for a, b in zip(_TOP, _BOTTOM, strict=True))
        )
    im = grad.resize((px, px)).convert("RGBA")

    mask = Image.new("L", (px, px), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, px - 1, px - 1], radius=8 * scale, fill=255)
    im.putalpha(mask)

    d = ImageDraw.Draw(im)
    d.polygon([pt(p) for p in _GEM], fill=(255, 255, 255, 255))
    for line in _FACET_LINES:
        d.line([pt(p) for p in line], fill=_FACET, width=max(1, round(1.1 * scale)), joint="curve")
    return im.resize((size, size), Image.LANCZOS)
