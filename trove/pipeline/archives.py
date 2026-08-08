"""Facts about one archive that the pipeline derives rather than stores.

Not to be confused with ``services/archives.py``, which is the *registry* --
which archives exist, where they live, adding and removing them. This module
answers two much narrower questions about an archive that is already open:
how many files are on disk under it, and whether it owes a duplicate rebuild.

Both are derived, and deliberately so. The disk count is cached because it is
the one expensive part of freshness; the dedup obligation is read back out of
the catalog rather than held in memory, so it survives a restart.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable

from ..config import Config
from ..db import database as db

logger = logging.getLogger(__name__)

# Freshness disk walk is reused for this long before re-walking. Comfortably
# below the scheduler's idle backoff so an idle tick still re-walks and notices
# files added to the source folder.
WALK_TTL = 60.0


class DiskCounts:
    """A per-root cache of "how many files are on disk", plus the background
    walk that refreshes it.

    Owns its own lock. The cache and the in-flight set are touched only from
    here, so they need no ordering against the job registry -- which is what
    the manager's ``_lock`` used to provide incidentally, not by design.
    """

    def __init__(self, stopping: Callable[[], bool]):
        self._cache: dict[int, tuple[float, int]] = {}
        # Roots with a background refresh walk in flight (see count()).
        self._inflight: set[int] = set()
        self._lock = threading.Lock()
        # One per root, held for the length of a blocking walk so that
        # concurrent cold callers share one walk instead of racing several.
        # See _walk. Never pruned: one lock per archive is not worth reclaiming.
        self._walks: dict[int, threading.Lock] = {}
        # Asked before starting a walk: on the way out there is no point
        # beginning one, and the thread would outlive the app.
        self._stopping = stopping

    def invalidate(self, root_id: int) -> None:
        """Drop a root's cached count. A finished scan changed what is on
        disk-vs-indexed, so freshness must reflect it immediately."""
        self._cache.pop(root_id, None)

    def count(
        self, root_id: int, root_path: str, max_age: float | None = None, allow_walk: bool = True
    ) -> int | None:
        """Files on disk under this root. None when there is no answer yet.

        ``allow_walk=False`` -- what the polled status endpoint passes -- means
        *this thread never walks*. It answers with the cached count, or with
        None when there is not one yet, and leaves a background walk running
        either way.

        None is the whole point of it. Counting 97k files on a cold cache takes
        about twenty seconds, and a walk on the request thread holds the reply
        for all of it; the browser keeps polling, and about six stalled requests
        later it has no connection left for thumbnails, grids or any other
        screen, so the whole archive looks frozen rather than merely slow to say
        what it is doing. This used to make an exception of the *first* walk --
        nothing cached to serve, and reporting "no idea" would have shown every
        stage as up to date -- but an archive that was just opened is always
        that case, so the exception was the common path. Saying "not counted
        yet" out loud costs one honest card (``stages`` reports the scan stage
        as ``checking``) and buys a status endpoint that cannot stall.

        ``allow_walk=True`` is the scheduler's path: it runs on its own thread,
        nothing waits on it, and it needs a real number to decide whether a scan
        is owed. That one still blocks, under the lock ``_walk`` describes.
        """
        ttl = WALK_TTL if max_age is None else max_age
        hit = self._cache.get(root_id)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        if not allow_walk:
            self._refresh(root_id, root_path)
            return hit[1] if hit else None
        return self._walk(root_id, root_path, ttl)

    def _walk(self, root_id: int, root_path: str, ttl: float) -> int | None:
        """Count the tree, one walk at a time per root.

        Opening an archive finds this cache empty, and three callers reach a
        cold root at once: the polled status endpoint, the scheduler tick
        (which passes ``allow_walk=True``), and every further poll that fires
        while the first is still counting. Without this lock each starts its
        own ``count_files`` over the same tree -- 97k files takes ~20s from a
        cold inode cache -- and walks contending for one disk make each other
        slower, so the pile drains more slowly the bigger it gets.

        The lock is held across the walk rather than around the cache writes:
        the point is for the second caller to *wait for the first walk's
        answer*, not to start a second one safely. Whoever takes it walks;
        whoever waits re-reads the cache and finds it filled. A caller with a
        fresh-enough count never reaches here, so a warm root is never made to
        queue behind a walk.
        """
        from pathlib import Path

        from ..scan.walker import count_files

        with self._walk_lock(root_id):
            hit = self._cache.get(root_id)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
            if not Path(root_path).is_dir():
                self._cache.pop(root_id, None)
                return None
            count = count_files(Path(root_path))
            self._cache[root_id] = (time.monotonic(), count)
            return count

    def _walk_lock(self, root_id: int) -> threading.Lock:
        """This root's walk lock, created on first use."""
        with self._lock:
            return self._walks.setdefault(root_id, threading.Lock())

    def _refresh(self, root_id: int, root_path: str) -> None:
        """Re-walk this root off the caller's thread, one walk at a time."""
        with self._lock:
            if root_id in self._inflight or self._stopping():
                return
            self._inflight.add(root_id)

        def run() -> None:
            try:
                self.count(root_id, root_path, max_age=0)
            except OSError:
                pass
            finally:
                with self._lock:
                    self._inflight.discard(root_id)

        threading.Thread(target=run, daemon=True).start()


def dedup_needed(cfg: Config, root_id: int) -> bool:
    """Whether a duplicate rebuild is outstanding for this root.

    Derived entirely from the catalog (db.dedup_needed / dedup_runs):
    a freshly constructed JobManager -- e.g. right after an app restart --
    gives the same answer a long-running one would have, instead of the
    old in-memory flag's default-dirty-on-every-start behaviour.
    """
    conn = db.open_readonly(cfg.archive_db_path(root_id))
    try:
        return db.dedup_needed(conn, root_id)
    finally:
        conn.close()


def mark_dedup_owed(cfg: Config, root_id: int) -> None:
    """Persist that a dedup rebuild is owed for this root.

    Scan can change the file set and enrich can populate metadata dedup's
    canonical-selection rule reads (Takeout sidecar, resolved date,
    dimensions) without changing the count/max id of present, hashed
    files `dedup_needed` otherwise compares (enrich never touches
    `files.sha256`/`present`) -- so that comparison alone would miss a
    case where the previous grouping's canonical pick is now stale.
    Invalidating through the same persisted marker the dedup runner writes on
    success means this obligation survives a restart too.

    Runs on the scheduler thread, which must not die on a transient lock:
    this tiny write can land while a detect batch holds the writer, and an
    escaping OperationalError would abort the whole tick before any stage
    starts. A lost invalidation is harmless — the next tick re-derives the
    same obligation and tries again.
    """

    def _write() -> None:
        conn = db.connect(cfg.archive_db_path(root_id))
        try:
            db.dedup_invalidate(conn, root_id)
            conn.commit()
        finally:
            conn.close()

    try:
        db.write_with_retry(_write)
    except sqlite3.OperationalError:
        # Not fatal -- the next tick marks it again -- but not nothing either:
        # while this keeps failing, dedup is not told the file set changed, so
        # "duplicates never rebuilt after a scan" is traced from here.
        logger.warning("could not mark dedup owed for root=%s", root_id, exc_info=True)
