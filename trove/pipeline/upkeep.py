"""The work the manager does *around* jobs rather than as jobs.

Three things live here, and what unites them is what they are not. None is a
pipeline stage, none has a ``Job``, none reports progress and nothing waits for
a result: they are the housekeeping that decides whether opening an archive
takes 175 ms or several seconds, and whether a file dropped into a folder is
noticed in seconds or on the next sweep. Each also owns a lock or a timer of its
own, which is the practical reason they were worth lifting out of
``manager.py`` -- that module's threading contract is about the job registry and
the write lock, and unrelated little state machines sitting beside it made it
read as though they were part of the same argument.

They stay callable exactly as before: ``JobManager`` keeps the public names
(``note_files_changed``, ``warm_for_open``) and delegates, because routes and
tests reach for them there.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

from ..db import database as db

logger = logging.getLogger(__name__)


def refresh_planner_stats(db_path: str) -> None:
    """Keep SQLite's query planner supplied with table statistics.

    Without ``sqlite_stat1`` the planner guesses at how selective each index
    is, and on this schema it guesses badly: the Overview's semantic
    breakdown chose to walk 97k files and fetch each one's row out of the
    169 MB embeddings table, taking 1218 ms to count three integers. With
    statistics it reads the index instead and takes 6 ms. Nothing about the
    query changed -- only what the planner knew.

    ``PRAGMA optimize`` rather than a bare ``ANALYZE``: it re-analyses only
    the tables whose contents have moved far enough from the recorded stats
    to matter. On a 97k-file archive that is ~660 ms the first time and
    ~1 ms on every job completion after it, which is what makes it
    affordable here rather than on some schedule nobody would tune.

    The two constants are measured, not folklore:

    * ``0x10002`` adds the "consider every table" bit to the usual analyse
      flag. Without it ``optimize`` only looks at tables *this connection*
      has queried, and this connection is opened to do nothing else -- it
      produced 17 of the 22 stat rows a full ANALYZE writes, missing the
      ones this is for. Older SQLite ignores the unknown bit and behaves as
      it does today.
    * ``analysis_limit`` bounds how much of each index ANALYZE reads.
      SQLite's suggested 400 is cheap but too coarse here: it costs the
      semantic count its good plan (356 ms instead of 258 ms, driving from
      `files` and doing 97k row lookups rather than reading 41k index
      entries). 50000 buys the right plan and still bounds the work on an
      archive much larger than this one.

    Best-effort, like the disk-count invalidation beside it. A job has
    already done its work and reported by the time this runs; a locked
    writer or a read-only file must not turn that into a failure.
    """
    try:
        conn = db.connect(db_path)
    except sqlite3.Error:
        return
    try:
        conn.execute("PRAGMA analysis_limit=50000")
        conn.execute("PRAGMA optimize=0x10002")
        conn.commit()
    except sqlite3.Error:
        logger.debug("could not refresh planner stats for %s", db_path, exc_info=True)
    finally:
        conn.close()


def warm_archives(db_paths: Callable[[], list[str]], stopping: Callable[[], bool]) -> None:
    """Pull what opening an archive is about to need into the page cache.

    Every failure here is swallowed: this has no result a caller is waiting
    for, so anything that goes wrong must cost only the speed it was trying to
    buy.

    Read-only and idempotent by construction -- it opens nothing for writing and
    calls nothing that migrates. ``_open_db`` still does all of that for real
    when the archive is opened; the only difference is that it finds the pages
    already in memory when it does.
    """
    try:
        from ..faces import migrate_adaface  # noqa: F401  (imported for its cost)

        for db_path in db_paths():
            if stopping():
                return
            if not Path(db_path).is_file():
                continue
            conn = db.open_readonly(db_path)
            try:
                # Reading the catalogue of tables and indexes is what pulls
                # in the pages init_db's forty CREATE ... IF NOT EXISTS
                # statements are about to check, one at a time.
                conn.execute("SELECT type, name FROM sqlite_master").fetchall()
                conn.execute("PRAGMA user_version").fetchone()
            finally:
                conn.close()
    except Exception:
        logger.debug("warm: gave up", exc_info=True)


class HintThrottle:
    """Per-root rate limit on "something changed on disk under here".

    Acting on a hint costs a walk of the whole tree, and files can arrive one
    at a time for as long as someone is dragging them in. A hint inside the
    floor is not dropped -- it is deferred to the end of it, so the last file
    of a slow trickle is still noticed without every file in the trickle
    costing a walk.

    ``act`` is what a hint actually does, which is deliberately not much: drop
    the cached disk count and wake the scheduler. Neither the watcher nor the
    window-focus hint is trusted to say *what* changed, and the tick that
    follows re-walks and decides -- which is why a hint being wrong,
    duplicated or absent costs nothing but timing.
    """

    def __init__(self, act: Callable[[int], None], floor: Callable[[], float]) -> None:
        self._act = act
        # Read per call, not captured. ``watcher.WALK_FLOOR`` is a module
        # attribute that tests rebind to keep a deferred hint inside a test's
        # patience, and a throttle holding the value it saw at construction
        # would ignore them -- silently, by waiting out the original floor.
        self._floor = floor
        self._lock = threading.Lock()
        self._at: dict[int, float] = {}
        self._timer: dict[int, threading.Timer] = {}

    def note(self, root_id: int, now: float) -> None:
        with self._lock:
            last = self._at.get(root_id)
            wait = 0.0 if last is None else self._floor() - (now - last)
            if wait > 0:
                if root_id not in self._timer:
                    timer = threading.Timer(wait, self._fire, args=(root_id,))
                    timer.daemon = True
                    self._timer[root_id] = timer
                    timer.start()
                return
            self._at[root_id] = now
        self._act(root_id)

    def _fire(self, root_id: int) -> None:
        """The tail of a throttled hint, once its floor has passed."""
        with self._lock:
            self._timer.pop(root_id, None)
        # Back through the front door: the floor has passed, so this call takes
        # the fast path and acts. Re-entering rather than acting directly keeps
        # one description of what a hint does.
        self.note(root_id, time.monotonic())

    def cancel(self) -> None:
        """Drop any deferred hints. Called on shutdown, where the scheduler is
        stopping and acting on one would be declined anyway."""
        with self._lock:
            for timer in self._timer.values():
                timer.cancel()
            self._timer.clear()
