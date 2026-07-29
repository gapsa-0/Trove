"""Runtime locations for mutable application data.

The installed package is read-only.  Resolve these paths when they are needed so
callers (and tests) can change the relevant environment variables at runtime.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "organize_archive"


def app_data_dir() -> Path:
    """Return the platform-appropriate per-user data directory."""
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / APP_NAME
        # LOCALAPPDATA is normally always set on Windows; keep a useful fallback
        # for unusual embedded/test environments.
        return Path.home() / "AppData" / "Local" / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_NAME


def config_file() -> Path:
    return app_data_dir() / "config.json"


def secrets_file() -> Path:
    """Locally stored, user-managed secrets (API keys).

    Kept out of ``config.json`` on purpose: it holds credentials, is written with
    owner-only permissions, and never leaves this machine.
    """
    return app_data_dir() / "secrets.json"


def default_db_path() -> Path:
    return app_data_dir() / "archive.db"


def default_cache_dir() -> Path:
    return app_data_dir() / "cache"


def archives_dir() -> Path:
    """Root of every isolated per-archive store (own db + own thumbnail cache)."""
    return app_data_dir() / "archives"


def archive_dir(archive_id: int) -> Path:
    return archives_dir() / str(archive_id)


def archive_db_path(archive_id: int) -> Path:
    return archive_dir(archive_id) / "archive.db"


def archive_cache_dir(archive_id: int) -> Path:
    return archive_dir(archive_id) / "cache"


def ensure_app_data_dirs() -> None:
    """Create the standard mutable-data layout when an operation needs it.

    Only ``models``/``icons`` live under the shared cache dir now, those are
    app binaries, not archive content. Thumbnails and face crops are per-archive
    and get created lazily under each archive's own cache dir.
    """
    data_dir = app_data_dir()
    for directory in (
        data_dir,
        default_cache_dir() / "models",
        default_cache_dir() / "icons",
        data_dir / "logs",
    ):
        directory.mkdir(parents=True, exist_ok=True)
