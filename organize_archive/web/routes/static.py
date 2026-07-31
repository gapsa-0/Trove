"""Routes that serve the app itself rather than an archive: the shell, its
assets, and the two endpoints that answer without opening a database."""

from __future__ import annotations

import os

from ... import __version__
from ._request import Request


def health(req: Request) -> dict:
    """Liveness plus the build this process is running, which is what the
    desktop shell polls for before showing a window."""
    return {
        "ok": True,
        "version": __version__,
        "commit": os.environ.get("ARCHIVE_BUILD_COMMIT", "dev"),
    }
