"""The archive registry: what archives exist, and adding or forgetting one.

The only service module that takes a `cfg` rather than a `db_path`, because
the registry itself lives in config.json and not in any one database. Why
that is, and what it means for add/remove, is the comment block below.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from .. import features as feature_catalog
from ..config import Config
from ..db import database as db
from ..paths import archive_dir as archive_dir_path
from . import models
from ._common import _NOT_HIDDEN, _VISIBLE

logger = logging.getLogger(__name__)

# -- archives ---------------------------------------------------------------
#
# Each archive is fully self-contained: its own database (cfg.archive_db_path),
# its own thumbnail/face-crop cache (cfg.archive_cache_dir). The registry of
# which archives exist lives in Config (config.json), not in any one database,
# since there is no longer a single shared database to hold a `roots` table
# for all of them.


def archives(cfg: Config) -> list[dict[str, Any]]:
    """Every registered archive with its picker-page stats: file and byte
    counts, last scan time and cover thumbnails, or zeroed stats when its
    database hasn't been created yet.

    Nothing here may read a column no index covers. This runs for every archive
    before the start page can draw a single card, with the page cache cold --
    the state the app opens in -- so a column fetched from the table means
    reading the whole table, which on a 97k-file archive is 263 MB and was
    0.7 seconds of staring at an empty page. Both figures below are answered
    from an index and never touch it (idx_files_browse, idx_files_size).
    """
    out: list[dict[str, Any]] = []
    for entry in sorted(cfg.archives, key=lambda a: a["id"]):
        rid, path = entry["id"], entry["path"]
        db_path = cfg.archive_db_path(rid)
        row: dict[str, Any] = {
            "id": rid,
            "path": path,
            "name": cfg.archive_name(rid),
            "features": list(cfg.archive_features(rid)),
            "added_at": entry["added_at"],
            "files": 0,
            "size": 0,
            "exists": Path(path).is_dir(),
            "last_scan": None,
            "covers": [],
        }
        if Path(db_path).is_file():
            conn = db.open_readonly(db_path)
            try:
                # Two aggregates, two indexes. A third once counted how many
                # files had been hashed, which nothing on the start page or
                # anywhere else ever showed -- and `sha256` is in no index, so
                # that unread number was what pulled the entire table off disk.
                stats = conn.execute(
                    f"""SELECT COUNT(*) c, COALESCE(SUM(size),0) s
                        FROM files f WHERE {_VISIBLE}"""
                ).fetchone()
                last = conn.execute(
                    "SELECT started_at, finished_at FROM scan_runs ORDER BY id DESC LIMIT 1"
                ).fetchone()
                # A few canonical image ids for the start-page cover mosaic. Served
                # by the root-scoped /archivethumb route (no archive is "open" on
                # the picker). Newest first so the cover reflects recent additions.
                covers = [
                    r[0]
                    for r in conn.execute(
                        f"""SELECT f.id FROM files f
                        WHERE {_NOT_HIDDEN} AND f.media_type='image'
                        ORDER BY f.id DESC LIMIT 5"""
                    ).fetchall()
                ]
                row.update(
                    files=stats["c"],
                    size=stats["s"],
                    covers=covers,
                    last_scan=(last["finished_at"] or last["started_at"]) if last else None,
                )
            finally:
                conn.close()
        out.append(row)
    return out


def features(cfg: Config) -> list[dict[str, Any]]:
    """The feature catalogue as the setup panel needs it.

    The static half comes from ``trove/features.py``; the two facts
    only a running installation knows come from ``services/models.py``, which is
    also what the fetch job downloads through, so the panel cannot quote one
    thing and the download turn out to be another. ``available`` is whether the
    optional dependency imports at all, and ``ready`` is whether the weights are
    already on this machine — they are shared between archives, so the second
    archive that wants People pays nothing, and a panel that still quoted 275 MB
    would be talking about a download that is not going to happen.
    """
    out: list[dict[str, Any]] = []
    for feature in feature_catalog.FEATURES:
        available = models.available(cfg, feature.id)
        out.append(
            {
                "id": feature.id,
                "label": feature.label,
                # The mark the panel draws beside the name, and the same one
                # the Overview card and nav section for this feature carry --
                # a key into the frontend's ICONS, resolved there.
                "icon": feature.icon,
                "tagline": feature.tagline,
                "detail": feature.detail,
                "required": feature.required,
                "download_mb": feature.download_mb,
                "pairs_with": feature.pairs_with,
                "sections": list(feature.sections),
                "available": available,
                # An unavailable feature is not "ready": nothing can be fetched
                # that would make it run, and only a feature with no download at
                # all is honestly nothing-to-wait-for.
                "ready": models.ready(cfg, feature.id) if available else not feature.download_mb,
            }
        )
    return out


def configure_archive(
    cfg: Config, root_id: int, name: str | None = None, chosen: list[str] | None = None
) -> dict[str, Any]:
    """Rename an archive and/or change which features it runs.

    Switching a feature off never deletes what it already found: its stage stops
    being scheduled and its section disappears, and switching it back on later
    picks up from the catalogue rather than starting again. That is why this is
    a registry write and nothing else.
    """
    if cfg.archive_path(root_id) is None:
        return {"error": "archive not found"}
    if name is not None:
        cfg.set_archive_name(root_id, name)
    enabled = (
        cfg.set_archive_features(root_id, chosen)
        if chosen is not None
        else cfg.archive_features(root_id)
    )
    return {"ok": True, "name": cfg.archive_name(root_id), "features": list(enabled)}


def check_folder(cfg: Config, path: str) -> dict[str, Any]:
    """Whether this folder could become an archive, without creating anything.

    Everything ``add_archive`` can refuse a folder *for* before it starts work,
    asked as its own question so the picker can ask it when the folder is
    chosen rather than after someone has configured an archive that was never
    going to be created. It is the same function both times, not a second
    opinion: the check that runs at creation is this one.

    ``archive_id`` comes back with the "already an archive" answer, because at
    that point the useful thing to know is which of the folders already on the
    start page this is.
    """
    p = Path(path).expanduser()
    if not p.is_dir():
        return {"error": f"Not a directory: {path}"}
    resolved = str(p.resolve())
    existing = next((a for a in cfg.archives if a["path"] == resolved), None)
    if existing is not None:
        return {"error": "That folder is already an archive.", "archive_id": existing["id"]}
    return {"ok": True, "path": resolved}


def add_archive(
    cfg: Config, path: str, name: str | None = None, chosen: list[str] | None = None
) -> dict[str, Any]:
    """Register a new archive rooted at ``path``, building its private database
    first. Returns ``{"id": ..., "path": ...}`` on success; an ``{"error":
    ...}`` dict if the path isn't a directory, is already registered, or the
    catalog can't be prepared."""
    refused = check_folder(cfg, path)
    if "error" in refused:
        return {"error": refused["error"]}
    resolved = refused["path"]
    # Build the private store first and register it only once it is usable, so a
    # failure here cannot leave a half-added archive in config.json that appears
    # out of nowhere on the next page load.
    #
    # The one mutation the routes do NOT wrap in db.write_with_retry, on purpose:
    # every other one writes a catalog that a pipeline job may be writing at the
    # same moment, which is what that retry is for. This one writes a database
    # file that did not exist a moment ago and whose path nothing else knows yet
    # -- there is no second writer to lose a race to. Retrying it would only add
    # a delay in front of a failure that is genuinely fatal (unwritable
    # directory, full disk), which the rmtree-and-report below already handles.
    aid = cfg.allocate_archive_id()
    try:
        conn = db.connect(cfg.archive_db_path(aid))
        try:
            db.init_db(conn)
            db.reconcile_root(conn, aid, resolved)
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("could not prepare catalog for %s", resolved, exc_info=True)
        shutil.rmtree(archive_dir_path(aid), ignore_errors=True)
        return {"error": f"Could not prepare a catalog for that folder: {exc}"}
    entry = cfg.register_archive(aid, resolved, name, chosen)
    return {
        "id": entry["id"],
        "path": resolved,
        "name": cfg.archive_name(aid),
        "features": list(cfg.archive_features(aid)),
    }


def remove_archive(cfg: Config, root_id: int) -> dict[str, Any]:
    """Forget one archive: delete its private database and cache wholesale.

    Nothing is shared between archives, so unlike the old shared-catalog
    design this never needs to touch any other archive's rows or cache files.
    """
    if not isinstance(root_id, int):
        return {"error": "root_id is required"}
    path = cfg.archive_path(root_id)
    if path is None:
        return {"error": "archive not found"}
    shutil.rmtree(archive_dir_path(root_id), ignore_errors=True)
    cfg.remove_archive_entry(root_id)
    return {"ok": True, "path": path}
