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


def default_db_path() -> Path:
    return app_data_dir() / "archive.db"


def default_cache_dir() -> Path:
    return app_data_dir() / "cache"


def ensure_app_data_dirs() -> None:
    """Create the standard mutable-data layout when an operation needs it."""
    data_dir = app_data_dir()
    for directory in (data_dir, default_cache_dir() / "thumbs",
                      default_cache_dir() / "models", data_dir / "logs"):
        directory.mkdir(parents=True, exist_ok=True)
