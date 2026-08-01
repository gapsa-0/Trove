"""Per-archive registry: id allocation, and one-time migration of the legacy
shared-catalog database into the current per-archive layout.

Provides ``ArchiveRegistryMixin``, mixed into ``Config`` (see ``settings.py``);
nothing here is used standalone.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..paths import archive_cache_dir, archive_db_path, archive_dir, archives_dir

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class ArchiveRegistryMixin:
    """Per-archive isolation.

    Each GUI archive is a fully self-contained store: its own database, its
    own thumbnail/face-crop cache. Only app-wide binaries (ML model weights,
    the app icon — see ``db_path``/``cache_dir`` on `Config`) are shared
    between archives.

    Mixed into `Config` only (see settings.py) -- it reads attributes it does
    not define here (``self.archives``, ``self.db_path``, ``self.cache_dir``,
    ``self.legacy_migrated``) and calls ``self.save()``, all of which `Config`
    provides.
    """

    if TYPE_CHECKING:
        # Provided by Config, which is the only class this is mixed into (see
        # the class docstring above). Type-only: zero runtime footprint.
        archives: list[dict[str, Any]]
        db_path: str
        cache_dir: str
        legacy_migrated: bool

        def save(self) -> None: ...

    def _next_archive_id(self) -> int:
        """The next never-used archive id.

        Deliberately counts the directories under ``archives/`` as well as the
        registry, so an id is not handed out again just because its entry was
        removed. A removal whose ``rmtree`` did not fully land (it is best
        effort) would otherwise point a brand-new archive at the *previous*
        archive's database, whose root row still names the old folder — the
        exact mismatch db.reconcile_root exists to clean up.
        """
        used = {a["id"] for a in self.archives}
        try:
            used |= {
                int(d.name) for d in archives_dir().iterdir() if d.is_dir() and d.name.isdigit()
            }
        except OSError:
            # No archives/ directory yet, on a fresh install: the registry above
            # is then the whole truth. Correct silence, not a swallowed failure.
            pass
        return max(used, default=0) + 1

    def archive_path(self, archive_id: int) -> str | None:
        return next((a["path"] for a in self.archives if a["id"] == archive_id), None)

    def archive_db_path(self, archive_id: int) -> str:
        return str(archive_db_path(archive_id))

    def archive_cache_dir(self, archive_id: int) -> str:
        return str(archive_cache_dir(archive_id))

    def allocate_archive_id(self) -> int:
        """Claim an id and create its private directory, registering nothing yet.

        Adding an archive is two writes — this directory tree and the registry
        entry — and the registry one goes last (register_archive), so a failure
        while building the database cannot leave config.json advertising an
        archive that was never set up. That ordering is why the picker no longer
        needs a full page reload to agree with what actually exists.
        """
        aid = self._next_archive_id()
        archive_dir(aid).mkdir(parents=True, exist_ok=True)
        return aid

    def register_archive(self, archive_id: int, path: str) -> dict:
        """Record a fully prepared archive in the registry."""
        entry = {"id": archive_id, "path": path, "added_at": _now_iso()}
        self.archives.append(entry)
        self.save()
        return entry

    def remove_archive_entry(self, archive_id: int) -> None:
        self.archives = [a for a in self.archives if a["id"] != archive_id]
        self.save()

    def migrate_legacy_archive(self) -> None:
        """One-time copy of the old shared single-catalog database into the new
        per-archive layout, so a catalog built before this version doesn't
        appear to vanish from the GUI. Runs at most once (``legacy_migrated``
        latches even when there was nothing to migrate).

        Only handles the common case of a legacy database with exactly one
        root — that is what every existing installation actually has. A legacy
        database with several roots predates per-archive isolation entirely
        and mixed their data in ways that can't be split back apart
        automatically (e.g. cross-root duplicate groups); migration is skipped
        and the folders must be re-added as separate archives instead.
        """
        if self.legacy_migrated:
            return
        self.legacy_migrated = True
        legacy_db = Path(self.db_path)
        if not legacy_db.is_file():
            self.save()
            return
        try:
            src = sqlite3.connect(str(legacy_db))
        except sqlite3.DatabaseError:
            self.save()
            return
        try:
            try:
                roots = src.execute("SELECT id, path FROM roots").fetchall()
            except sqlite3.DatabaseError:
                roots = []
            if len(roots) == 1:
                _legacy_root_id, root_path = roots[0]
                aid = self.allocate_archive_id()
                # A plain file copy could miss pages the legacy database hasn't
                # checkpointed out of its WAL yet if it's open elsewhere (e.g. a
                # running GUI). Back it up through SQLite instead, so the copy is
                # always a consistent snapshot regardless of concurrent access.
                dst = sqlite3.connect(str(archive_db_path(aid)))
                try:
                    src.backup(dst)
                    # The copy keeps the *legacy* root id, which has no reason to
                    # match the archive id this store is now filed under. Left
                    # alone that silently hides the whole catalogue from a GUI
                    # that only ever asks for `aid`.
                    from ..db import database as _db

                    dst.row_factory = sqlite3.Row
                    dst.execute("PRAGMA foreign_keys=ON")
                    _db.reconcile_root(dst, aid, root_path)
                finally:
                    dst.close()
                legacy_cache = Path(self.cache_dir)
                for sub in ("thumbs", "faces"):
                    cache_src = legacy_cache / sub
                    if cache_src.is_dir():
                        # dirs_exist_ok: this whole method must stay safe to
                        # re-run (see the hard "resumable & idempotent" rule)
                        # -- a prior attempt may have partially copied before
                        # crashing or being interrupted, leaving the target
                        # directory already present.
                        shutil.copytree(cache_src, archive_cache_dir(aid) / sub, dirs_exist_ok=True)
                self.archives.append({"id": aid, "path": root_path, "added_at": _now_iso()})
            elif len(roots) > 1:
                # Logged, not printed: this runs inside Config.load(), which the
                # desktop backend calls too, where a print goes to a stdout the
                # shell only scans for the READY handshake and then discards.
                logger.warning(
                    "%s holds %d roots from the previous shared-catalog design. "
                    "Each archive now needs its own database, so automatic "
                    "migration was skipped; re-add those folders as separate "
                    "archives in the GUI.",
                    legacy_db,
                    len(roots),
                )
        finally:
            src.close()
        self.save()
