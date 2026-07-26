"""Regenerate the packaged desktop icons from the one canonical Trove mark.

The web app draws the mark inline (``#trove-mark`` in gui/index.html) and the
PWA icon is generated on demand by ``gui/icons.py``, but electron-builder needs
real files on disk. Rather than hand-drawing a third copy, render those files
from the SAME ``icons._render`` geometry:

    python tools/render_logo.py

Re-run it after any change to the mark, and commit the regenerated files.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw          # noqa: E402

from organize_archive.gui import icons    # noqa: E402

BUILD = Path(__file__).resolve().parents[1] / "desktop" / "build"
# The sizes Windows actually picks between; electron-builder wants them all in
# the one .ico, and the taskbar/alt-tab ends are the small ones.
ICO_SIZES = (16, 32, 48, 64, 128, 256)


def main() -> int:
    BUILD.mkdir(parents=True, exist_ok=True)
    png = BUILD / "icon.png"
    icons._render(Image, ImageDraw, 1024).save(png, "PNG")
    # Render each .ico member at its own size instead of letting Pillow
    # downscale one bitmap: the facet lines are hairlines at 16px and survive a
    # dedicated supersampled render far better than a resize of the 256px one.
    largest = icons._render(Image, ImageDraw, max(ICO_SIZES))
    largest.save(BUILD / "icon.ico", "ICO", sizes=[(s, s) for s in ICO_SIZES],
                 append_images=[icons._render(Image, ImageDraw, s)
                                for s in ICO_SIZES if s != max(ICO_SIZES)])
    print(f"wrote {png} and {BUILD / 'icon.ico'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
