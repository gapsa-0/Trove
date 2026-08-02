"""The archive registry: what archives exist, and adding or forgetting one.

The only service module that takes a `cfg` rather than a `db_path`, because
the registry itself lives in config.json and not in any one database. Why
that is, and what it means for add/remove, is the comment block below.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any

from ..config import Config
from ..db import database as db
from ..paths import archive_dir as archive_dir_path
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
    """Every registered archive with its picker-page stats: file/byte/hashed
    counts, last scan time and cover thumbnails, or zeroed stats when its
    database hasn't been created yet."""
    out: list[dict[str, Any]] = []
    for entry in sorted(cfg.archives, key=lambda a: a["id"]):
        rid, path = entry["id"], entry["path"]
        db_path = cfg.archive_db_path(rid)
        row: dict[str, Any] = {
            "id": rid,
            "path": path,
            "name": os.path.basename(path.rstrip("/")) or path,
            "added_at": entry["added_at"],
            "files": 0,
            "size": 0,
            "hashed": 0,
            "exists": Path(path).is_dir(),
            "last_scan": None,
            "covers": [],
        }
        if Path(db_path).is_file():
            conn = db.open_readonly(db_path)
            try:
                stats = conn.execute(
                    f"""SELECT COUNT(*) c, COALESCE(SUM(size),0) s,
                               SUM(sha256 IS NOT NULL) hashed
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
                    hashed=stats["hashed"] or 0,
                    covers=covers,
                    last_scan=(last["finished_at"] or last["started_at"]) if last else None,
                )
            finally:
                conn.close()
        out.append(row)
    return out


def add_archive(cfg: Config, path: str) -> dict[str, Any]:
    """Register a new archive rooted at ``path``, building its private database
    first. Returns ``{"id": ..., "path": ...}`` on success; an ``{"error":
    ...}`` dict if the path isn't a directory, is already registered, or the
    catalog can't be prepared."""
    p = Path(path).expanduser()
    if not p.is_dir():
        return {"error": f"Not a directory: {path}"}
    resolved = str(p.resolve())
    if any(a["path"] == resolved for a in cfg.archives):
        return {"error": "That folder is already an archive."}
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
    entry = cfg.register_archive(aid, resolved)
    return {"id": entry["id"], "path": resolved}


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
