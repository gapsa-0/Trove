"""Locate optional executables and model weights bundled with a desktop backend."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def tool(name: str) -> str | None:
    """Return a bundled tool first, then a normal PATH lookup."""
    suffix = ".exe" if sys.platform.startswith("win") else ""
    roots = [os.environ.get("ARCHIVE_TOOLS_DIR")]
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        roots.append(str(Path(frozen_root) / "tools"))
    for root in roots:
        if root:
            candidate = Path(root) / f"{name}{suffix}"
            if candidate.is_file():
                return str(candidate)
    return shutil.which(name)


def bundled_model(relative_path: str) -> Path | None:
    """Return a model shipped inside the frozen build, if there is one.

    Only weights with no upstream download URL travel this way (see
    ``packaging/models/manifest.json``); everything else is fetched once into the
    cache at first run. Returns None in a source checkout.
    """
    roots = [os.environ.get("ARCHIVE_MODELS_DIR")]
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        roots.append(str(Path(frozen_root) / "models"))
    for root in roots:
        if root:
            candidate = Path(root) / relative_path
            if candidate.is_file():
                return candidate
    return None
