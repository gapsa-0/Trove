"""The vocabulary a runner is written against: one job, its progress, and the
context it is handed.

This module exists so ``manager.py`` and ``runners/`` can share these types
without importing each other: the manager dispatches *into* the runners, so
anything the runners need from it would close a cycle. Nothing here imports
either of them, and nothing here touches a thread -- the manager owns all of
that (see its module docstring for the threading contract).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from ..config import Config


@dataclass
class Job:
    id: int
    kind: str  # "scan" | "enrich" | "dedup" | "places" | "detect" |
    # "face_cluster" | "pet_cluster" | "semantic"
    root_id: int | None
    root_path: str | None
    force: bool = False
    status: str = "running"  # running | done | error | cancelled
    total: int = 0
    done: int = 0
    current: str = ""
    message: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def public(self) -> dict:
        d = asdict(self)
        d["percent"] = round(100 * self.done / self.total, 1) if self.total else None
        d["elapsed"] = round((self.finished_at or time.time()) - self.started_at, 1)
        return d


class JobProgress:
    """Adapter with the interface walker/enrich expect (.total, .update()).

    ``base`` / ``fixed_total`` let a multi-pass job (faces: detect in chunks,
    re-clustering between them) present one continuous bar: each pass reports
    0..chunk offset by ``base``, while the grand ``total`` stays put instead of
    being reset to the chunk size on every pass."""

    def __init__(self, job: Job, cancel: threading.Event, base: int = 0, fixed_total: bool = False):
        self.job = job
        self._cancel = cancel
        self.base = base
        self._fixed_total = fixed_total

    @property
    def total(self):
        return self.job.total

    @total.setter
    def total(self, v):
        if not self._fixed_total:
            self.job.total = v or 0

    def update(self, done, _bytes=0, current=""):
        if self._cancel.is_set():
            raise KeyboardInterrupt
        self.job.done = self.base + done
        if current:
            self.job.current = current

    def close(self):
        pass


@dataclass(frozen=True)
class JobContext:
    """Everything a runner is allowed to touch, and nothing else.

    A runner never sees the ``JobManager``. That is deliberate: it is what keeps
    "how this kind of work is done" free of "when it runs and on which thread",
    and it is why a runner can be exercised in a test with a plain connection
    and a fresh ``threading.Event``.

    ``conn`` is ``None`` only for a runner that declares ``needs_connection =
    False`` and opens its own (semantic does, because it snapshots under a
    read-only connection and writes each result in its own tiny transaction).
    """

    cfg: Config
    job: Job
    cancel: threading.Event
    conn: sqlite3.Connection | None
    log: logging.Logger

    def progress(self, base: int = 0, fixed_total: bool = False) -> JobProgress:
        """A progress adapter bound to this job and this job's cancel event.

        Runners call this rather than constructing ``JobProgress`` themselves so
        that the cancel event cannot be forgotten -- a progress object wired to
        the wrong event, or to none, is a job that ignores cancellation, and
        that is the one failure mode that makes the app un-quittable.
        """
        return JobProgress(self.job, self.cancel, base=base, fixed_total=fixed_total)

    def raise_if_cancelled(self) -> None:
        """Bail out of a long loop at a checkpoint.

        ``KeyboardInterrupt`` rather than a custom exception because that is
        what ``JobProgress.update`` already raises, and the manager records the
        two identically as "cancelled; progress saved". A runner that swallows
        it, or that never calls this in its loop, breaks the resumability rule.
        """
        if self.cancel.is_set():
            raise KeyboardInterrupt


@dataclass(frozen=True)
class Runner:
    """One kind of background work, plus how the manager must set it up.

    The two flags are declarations, not preferences -- they describe what this
    kind of work does to the database, and the manager reads them to decide
    what to hand over:

    * ``takes_write_lock`` -- hold the single-writer lock for the whole run.
      True for the stages that rewrite tables wholesale (dedup, places, detect
      and the two re-clustering jobs); False for the ones that commit in small
      batches and are safe to overlap under WAL (scan, enrich, semantic).
    * ``needs_connection`` -- the manager opens a connection, hands it over in
      ``ctx.conn`` and closes it afterwards. False only for semantic, which
      manages its own.

    Keeping them here rather than in the manager means adding a stage cannot
    quietly acquire the wrong locking behaviour: the declaration sits next to
    the code it describes.
    """

    kind: str
    run: Callable[[JobContext], None]
    takes_write_lock: bool = True
    needs_connection: bool = True
