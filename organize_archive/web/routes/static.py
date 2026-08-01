"""Routes that serve the app itself rather than an archive: the shell, its
assets, and the two endpoints that answer without opening a database."""

from __future__ import annotations

import json
import os

from ... import __version__
from .. import assets, icons
from ._request import NOT_FOUND, FileBody, Json, Raw, Request


def health(req: Request) -> dict:
    """Liveness plus the build this process is running, which is what the
    desktop shell polls for before showing a window."""
    return {
        "ok": True,
        "version": __version__,
        "commit": os.environ.get("ARCHIVE_BUILD_COMMIT", "dev"),
    }


def settings(req: Request) -> dict:
    return {}


def index(req: Request) -> FileBody:
    # Never cache the app shell, so a server update takes effect on a plain
    # reload (no hard-refresh needed to shake off stale JS).
    return FileBody(assets.INDEX, "text/html; charset=utf-8", cache_control="no-store")


def manifest(req: Request) -> Raw:
    return Raw(json.dumps(assets.MANIFEST).encode(), "application/manifest+json")


def service_worker(req: Request) -> Raw:
    return Raw(assets.SW.encode(), "text/javascript")


def icon(req: Request) -> Raw:
    size = 512 if "512" in req.path else 192
    return Raw(icons.app_icon(req.cfg.cache_dir, size), "image/png")


def vendor(req: Request) -> FileBody | Json:
    name = req.path.rsplit("/", 1)[1]
    vf = assets.VENDOR_DIR / name
    if vf.is_file() and ".." not in name:
        return FileBody(vf)
    return NOT_FOUND
