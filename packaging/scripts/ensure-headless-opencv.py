#!/usr/bin/env python3
"""Leave exactly one OpenCV in a build environment: the headless one.

``packaging/requirements-desktop.txt`` pins ``opencv-python-headless`` -- the same
OpenCV, built without highgui -- and two of the packages beside it in that same
file, ``insightface`` and ``rapidocr``, declare ``opencv-python`` instead. pip
honours all three requirements, because they are not in conflict: it installs
both distributions, they write the same ``cv2/`` package, and whichever pip
unpacked last is the one that answers ``import cv2`` and the one PyInstaller
freezes.

On the 0.3.0 release runners that was the full wheel. Every installer carried
Qt5 Core, Gui, Widgets and XcbQpa, the xcb/xkb stack under ``cv2/qt/plugins``,
and a second copy of ``libav*``: 36 MB raw, about 13 MB of download, in a
program that never opens a cv2 window. ``tests/unit/test_opencv_headless.py``
stayed green the whole time: it asks whether the *code* calls a GUI function,
which is a different question from which wheel is installed, and both are worth
asking.

pip has no way to say "satisfy that dependency with this distribution", so undo
the outcome instead of trying to prevent it: uninstall the full wheel, then
reinstall the headless one, because the two share files and removing either
takes the shared ``cv2/`` with it. Nothing else in the environment notices --
insightface and rapidocr both simply ``import cv2``.

Run it after every ``pip install`` on a machine that will freeze a build.
``packaging/trove.spec`` fails the build if it was not: the Qt payload is easier
to detect than to explain once shipped.
"""

from __future__ import annotations

import argparse
import importlib.metadata as metadata
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REQUIREMENTS = ROOT / "packaging" / "requirements-desktop.txt"
FULL = "opencv-python"
HEADLESS = "opencv-python-headless"


def installed(name: str) -> str | None:
    try:
        return metadata.distribution(name).version
    except metadata.PackageNotFoundError:
        return None


def pinned_headless_version() -> str:
    """The version requirements-desktop.txt asks for, so this file pins nothing.

    A second copy of the number here would be one more thing to bump, and the
    kind that goes unnoticed: installing a *different* OpenCV than the desktop
    profile names would still produce a working build.
    """
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(rf"\s*{re.escape(HEADLESS)}==([\w.]+)\s*", line)
        if match:
            return match.group(1)
    raise SystemExit(f"error: no {HEADLESS}== pin in {REQUIREMENTS.relative_to(ROOT)}")


def pip(*arguments: str) -> None:
    subprocess.run([sys.executable, "-m", "pip", *arguments], check=True)


def gui_backend() -> str:
    """What OpenCV itself says it was built with -- the only answer that counts.

    Reading the metadata says which distributions pip recorded; this says which
    one is actually on the import path, which is the thing that gets frozen.
    """
    # Imported here rather than at module scope because importing it is the check.
    import cv2

    for line in cv2.getBuildInformation().splitlines():
        if line.strip().startswith("GUI:"):
            return line.split(":", 1)[1].strip()
    return "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="report the state and fail if the full wheel is installed, changing nothing",
    )
    arguments = parser.parse_args()

    full, headless = installed(FULL), installed(HEADLESS)
    if full and arguments.check:
        print(f"error: {FULL} {full} is installed; run packaging/scripts/ensure-headless-opencv.py")
        return 1
    if full:
        version = headless or pinned_headless_version()
        print(f"removing {FULL} {full}; {HEADLESS} {version} stays")
        pip("uninstall", "--yes", FULL)
        # The uninstall above took the shared cv2/ with it, whether or not the
        # headless distribution still claims those files, so this is a repair
        # and not a no-op -- hence --force-reinstall. --no-deps because numpy is
        # already resolved and this must not become a second resolution.
        pip("install", "--force-reinstall", "--no-deps", f"{HEADLESS}=={version}")
    elif not headless:
        print(f"error: neither {FULL} nor {HEADLESS} is installed")
        return 1

    backend = gui_backend()
    print(f"{HEADLESS} {installed(HEADLESS)}, OpenCV GUI backend: {backend}")
    if backend.upper() not in {"NONE", "UNKNOWN"}:
        print(f"error: the OpenCV on the import path was built with {backend}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
