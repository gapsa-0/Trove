"""Locate optional executables and model weights bundled with a desktop backend."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def no_window() -> dict:
    """subprocess kwargs that keep a console tool from flashing a window.

    The Windows desktop build is a GUI-subsystem exe (see the ``console=``
    argument in ``packaging/trove.spec``), so it has no console of
    its own. Windows then allocates a *fresh* console window for every
    console-subsystem child — exiftool, ffmpeg — which pops up and vanishes.
    Over a pipeline run that is tens of thousands of flashes, and it makes the
    app look like something running commands behind the user's back.

    Empty on every other platform, where a child just inherits stdio.
    """
    if sys.platform.startswith("win"):
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def tool_roots(env: dict | None = None) -> list[str]:
    """The directories a bundled tool may live in, most specific first.

    ``env`` exists so ``tool_env`` resolves the roots from the *same* environment
    it is building, rather than from the current process's: the staging script
    points ARCHIVE_TOOLS_DIR at a directory this process has never run from.
    """
    source = os.environ if env is None else env
    roots = [source.get("ARCHIVE_TOOLS_DIR")]
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        roots.append(str(Path(frozen_root) / "tools"))
    return [root for root in roots if root]


def tool(name: str) -> str | None:
    """Return a bundled tool first, then a normal PATH lookup."""
    suffix = ".exe" if sys.platform.startswith("win") else ""
    for root in tool_roots():
        candidate = Path(root) / f"{name}{suffix}"
        if candidate.is_file():
            return str(candidate)
    return shutil.which(name)


def tool_env(base: dict | None = None) -> dict:
    """Environment for spawning a bundled tool, so it can find its own libraries.

    ffmpeg is bundled as a *shared* build -- small binaries beside the
    ``libav*``/``av*`` libraries they link against -- because the static build
    shipped the entire codec set twice, once inside ffmpeg and once inside
    ffprobe, for 266 MB. Sharing them costs one environment variable.

    Only Linux needs it. Windows resolves a DLL from the directory its .exe was
    loaded from, and ``stage-tools.py`` stages the DLLs exactly there.

    Two details that look optional and are not:

    * The staged directory is *prepended*, not appended. A frozen build inherits
      a ``LD_LIBRARY_PATH`` pointing into PyInstaller's ``_internal``, which
      carries OpenCV's own bundled ``libav*``. Appending would let a foreign
      libavcodec answer first.
    * The upstream build's RPATH cannot be relied on. BtbN's shared binaries ship
      ``RPATH=-Wl:../lib`` -- a quoting bug in their link flags, not the intended
      ``$ORIGIN/../lib`` -- so nothing resolves without this.

    Returns a full environment dict suitable for ``subprocess(env=...)``; pass
    ``base`` to build on something other than the current environment.
    """
    env = dict(os.environ if base is None else base)
    if sys.platform.startswith("win"):
        return env
    roots = tool_roots(env)
    if not roots:
        return env
    existing = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = os.pathsep.join([*roots, existing] if existing else roots)
    return env


def bundled_model(relative_path: str) -> Path | None:
    """Return a model shipped inside the frozen build, if there is one.

    Only weights with no upstream download URL travel this way (see
    ``packaging/models/manifest.json``); everything else is fetched once into the
    cache at first run. Returns None in a source checkout, where
    ``model_manifest.present`` carries on to the staged dir, the cache and the
    manifest's own URL -- this is the first of those four sources, not the only
    one, and callers should be asking that module rather than this function.
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
