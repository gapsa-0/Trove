"""Open the GUI in a standalone (app-mode) window when possible.

Chromium-family browsers (Chrome, Chromium, Edge, Brave) support `--app=URL`,
which opens a window with no tabs or address bar — it looks like a separate
program. Falls back to a normal browser tab when none is found.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import webbrowser

from ..runtime import no_window

logger = logging.getLogger(__name__)

# Names to try on PATH (Linux/macOS, and Windows where present).
_NAMES = [
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "brave-browser",
    "microsoft-edge",
    "microsoft-edge-stable",
    "chrome",
    "msedge",
]


def _windows_paths() -> list[str]:
    pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    pfx86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    out = [
        rf"{pf}\Google\Chrome\Application\chrome.exe",
        rf"{pfx86}\Google\Chrome\Application\chrome.exe",
        rf"{local}\Google\Chrome\Application\chrome.exe",
        rf"{pfx86}\Microsoft\Edge\Application\msedge.exe",
        rf"{pf}\Microsoft\Edge\Application\msedge.exe",
    ]
    return [p for p in out if p and os.path.isfile(p)]


def find_chromium() -> str | None:
    for name in _NAMES:
        p = shutil.which(name)
        if p:
            return p
    if sys.platform.startswith("win"):
        paths = _windows_paths()
        if paths:
            return paths[0]
    return None


def open_app_window(url: str) -> bool:
    """Try to open a chrome-less app window. Return True on success."""
    browser = find_chromium()
    if not browser:
        return False
    try:
        # A no-op for a GUI-subsystem browser, which never gets a console
        # allocated anyway -- passed so the "every spawn suppresses a console"
        # rule holds with no exceptions to remember (see tests/unit/test_no_console_windows.py).
        subprocess.Popen(
            [browser, f"--app={url}", "--new-window"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **no_window(),
        )
        return True
    except Exception:
        # Falling back to another browser (webbrowser.open, below) is normal
        # operation here, not a fault -- DEBUG keeps it out of the way while
        # still being there if "why did app-mode not open" needs answering.
        logger.debug("could not open app-mode window with %s", browser, exc_info=True)
        return False


def open_url(url: str, app_mode: bool = True) -> str:
    """Open the GUI. Returns a short description of how it was opened."""
    if app_mode and open_app_window(url):
        return "app-window"
    webbrowser.open(url)
    return "browser-tab"
