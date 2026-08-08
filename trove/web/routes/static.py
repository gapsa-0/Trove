"""Routes that serve the app itself rather than an archive: the shell, its
assets, and the two endpoints that answer without opening a database."""

from __future__ import annotations

import json
import os
import re

from ... import __version__, translation
from .. import assets, icons
from ._request import NOT_FOUND, FileBody, Json, Raw, Request

_APP_ASSET = re.compile(r"(?P<kind>css|js)/(?P<name>[A-Za-z0-9_.-]+\.(?:css|js))")


def health(req: Request) -> dict:
    """Liveness plus the build this process is running, which is what the
    desktop shell polls for before showing a window."""
    return {
        "ok": True,
        "version": __version__,
        "commit": os.environ.get("ARCHIVE_BUILD_COMMIT", "dev"),
    }


def settings(req: Request) -> dict:
    """No user-configurable settings exist yet; always answers with an empty object."""
    return {}


def index(req: Request) -> FileBody:
    """The single-page app shell HTML."""
    # Never cache the app shell, so a server update takes effect on a plain
    # reload (no hard-refresh needed to shake off stale JS).
    return FileBody(assets.INDEX, "text/html; charset=utf-8", cache_control="no-store")


def manifest(req: Request) -> Raw:
    """The PWA manifest describing the installable app."""
    return Raw(json.dumps(assets.MANIFEST).encode(), "application/manifest+json")


def service_worker(req: Request) -> Raw:
    """The service worker script that lets the app be installed and work offline."""
    return Raw(assets.SW.encode(), "text/javascript")


def icon(req: Request) -> Raw:
    """The app icon PNG, 512px or 192px depending on which the request path names."""
    size = 512 if "512" in req.path else 192
    return Raw(icons.app_icon(req.cfg.cache_dir, size), "image/png")


def app_asset(req: Request) -> FileBody | Json:
    """One of the app's own stylesheets or scripts, by ``css/<name>`` or ``js/<name>``.

    The path is matched against an allowlist pattern rather than resolved and
    range-checked: two fixed directory names and a filename that may hold only
    ``[A-Za-z0-9_.-]`` cannot express a traversal at all, encoded or not, so
    there is nothing to get wrong later. The content type is stated here rather
    than guessed, because a browser refuses an ES module served under the wrong
    MIME type and says so only in the console.
    """
    m = _APP_ASSET.fullmatch(req.path.removeprefix("/static/"))
    if not m:
        return NOT_FOUND
    path = assets.STATIC_DIR / m["kind"] / m["name"]
    if not path.is_file():
        return NOT_FOUND
    ctype = "text/css" if m["kind"] == "css" else "text/javascript"
    # Same reasoning as the shell itself: never cache, so a reload picks up a
    # server update without a hard refresh. These are local files on a local
    # server; there is no bandwidth to save.
    return FileBody(path, f"{ctype}; charset=utf-8", cache_control="no-store")


def vendor(req: Request) -> FileBody | Json:
    """A vendored static asset by filename, or 404 if it isn't one.

    Two directories answer here, and the split is by size rather than by kind.
    The small files -- Leaflet, the Bergamot loader and worker scripts -- ship
    in the package. The translator's four large ones are downloaded with Search
    by description and live in the model cache (``trove/translation.py``), so a
    user who never turns that feature on never carries them.

    A 404 for one of those four is a normal state, not an error: it is what
    "not downloaded yet" looks like, and the page treats a translator it cannot
    load as a query it does not expand.
    """
    name = req.path.rsplit("/", 1)[1]
    if ".." in name:
        return NOT_FOUND
    vf = assets.VENDOR_DIR / name
    if vf.is_file():
        return FileBody(vf)
    fetched = translation.resolve(name, req.cfg.cache_dir)
    return FileBody(fetched) if fetched else NOT_FOUND
