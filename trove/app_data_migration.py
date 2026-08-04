"""One-time move of the pre-rename application-data directory.

Until the rename, this application was called ``organize_archive`` and so was
its per-user data directory. An install that predates the rename keeps its
catalogue, config, cache and logs under that old name, in the same parent
directory the new one lives in. This module moves it across, once, at startup.

Two things make it more than a directory rename:

* ``Config.save()`` persists ``db_path`` and ``cache_dir`` as *absolute*
  strings, so moving the directory alone would leave a working install
  pointing at a catalogue path that no longer exists. They are repointed here,
  and only when they actually referred to the old directory -- an archive
  folder the user chose, or a database someone deliberately put on another
  volume, is none of this function's business.
* The move must never overwrite or merge anything. If both directories hold a
  catalogue -- someone ran a pre-rename and a post-rename build side by side --
  neither is touched and the collision is reported instead.

Doing nothing is by far the most common outcome: every fresh install, and
every subsequent launch of a migrated one, returns at the first two checks.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from .paths import app_data_dir, legacy_app_data_dir

logger = logging.getLogger(__name__)

# What makes a directory an install's data rather than an empty shell.
# ``logs/`` is deliberately absent: ``logging_setup`` creates it on the first
# record written, which happens before this runs, so counting it would make
# every launch look like a collision and block the migration forever.
_DATA_MARKERS = ("config.json", "secrets.json", "archive.db", "archives", "cache")


def _holds_data(directory: Path) -> bool:
    return any((directory / name).exists() for name in _DATA_MARKERS)


def migrate_legacy_app_data() -> bool:
    """Move a pre-rename data directory into place. True if anything moved.

    Safe to call on every startup and from either entry point: it is a no-op
    once there is nothing left under the old name.
    """
    legacy = legacy_app_data_dir()
    target = app_data_dir()
    if legacy == target or not legacy.is_dir() or not _holds_data(legacy):
        return False

    if _holds_data(target):
        logger.warning(
            "application data exists under both the old name (%s) and the new one (%s). "
            "Leaving both untouched; %s is the one in use. Move anything you still "
            "need out of the other by hand.",
            legacy,
            target,
            target,
        )
        return False

    try:
        _move(legacy, target)
    except OSError as exc:
        # A half-finished move is the one outcome worth being loud about: the
        # catalogue may now be split across two directories.
        logger.error(
            "could not move application data from %s to %s: %s. "
            "Trove will start against %s; the old directory was left in place.",
            legacy,
            target,
            exc,
            target,
        )
        return False

    _repoint_config(target, legacy)
    logger.info("moved application data from %s to %s after the rename to Trove", legacy, target)
    return True


def _move(legacy: Path, target: Path) -> None:
    """Move ``legacy`` onto ``target``, whether or not ``target`` exists yet.

    The normal case is a plain rename -- the two are siblings, so it is atomic
    and instant no matter how large the catalogue is. It fails when the target
    already exists, which it does as soon as anything has been logged, so the
    fallback moves each entry across individually.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        try:
            legacy.rename(target)
            return
        except OSError:
            # Either the target appeared between that check and this call, or
            # the two names resolve to different filesystems. Fall through.
            pass

    target.mkdir(parents=True, exist_ok=True)
    for entry in sorted(legacy.iterdir()):
        destination = target / entry.name
        if destination.exists():
            # Only ``logs/`` realistically gets here, and a merge is not worth
            # the risk of clobbering. The leftovers are reported below.
            continue
        shutil.move(str(entry), str(destination))

    leftovers = sorted(path.name for path in legacy.iterdir())
    if leftovers:
        logger.info("left %s behind in %s", ", ".join(leftovers), legacy)
    else:
        legacy.rmdir()


def _repoint_config(target: Path, legacy: Path) -> None:
    """Rewrite the absolute paths in config.json that pointed into ``legacy``."""
    config = target / "config.json"
    if not config.is_file():
        return
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # The move already succeeded, so this is recoverable by hand and must
        # not stop the application starting.
        logger.warning("could not reread %s after moving it: %s", config, exc)
        return
    if not isinstance(data, dict):
        return

    changed = False
    for key in ("db_path", "cache_dir"):
        value = data.get(key)
        if not isinstance(value, str) or not value:
            continue
        try:
            relative = Path(value).relative_to(legacy)
        except ValueError:
            continue  # somewhere else entirely; the user put it there on purpose
        data[key] = str(target / relative)
        changed = True

    if not changed:
        return
    try:
        # indent=2 to match Config.save(), so this does not show up as a
        # whole-file reformat the next time the config is written.
        config.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("could not repoint the paths in %s: %s", config, exc)
