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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any

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
    # True while the runner is inside a call that cancellation cannot reach --
    # in practice, building an ONNX session. Nothing can interrupt native code,
    # so ``shutdown`` stops *waiting* on such a job instead of spending its
    # whole timeout on a thread that cannot answer. Internal to the pipeline;
    # ``public()`` drops it rather than growing the polled job payload.
    uninterruptible: bool = False

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("uninterruptible")
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
    def total(self) -> int:
        return self.job.total

    @total.setter
    def total(self, v: int) -> None:
        if not self._fixed_total:
            self.job.total = v or 0

    def update(self, done: int, _bytes: int = 0, current: str = "") -> None:
        if self._cancel.is_set():
            raise KeyboardInterrupt
        self.job.done = self.base + done
        if current:
            self.job.current = current

    def close(self) -> None:
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

    def require_conn(self) -> sqlite3.Connection:
        """``ctx.conn``, narrowed to non-optional for the runners that need it.

        Every runner except ``semantic`` declares ``needs_connection = True``
        (the default -- see ``Runner``), and the manager only ever calls a
        runner without one when it declared it did not need it (``_dispatch``).
        So by the time any other runner's ``run()`` reaches this call, ``conn``
        is never ``None`` -- the ``RuntimeError`` below is unreachable under
        that contract and exists only so a future runner that starts using
        this while wrongly leaving ``needs_connection = False`` fails loudly
        instead of hitting ``AttributeError`` on ``None`` deep in a query.
        """
        if self.conn is None:
            raise RuntimeError(
                "JobContext.conn is None -- this runner must declare needs_connection = True"
            )
        return self.conn

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

    @contextmanager
    def uninterruptible(self, what: str) -> Iterator[None]:
        """Run a section that cancellation provably cannot reach.

        Loading an ONNX model is one native call of several seconds with no
        checkpoint inside it, so a job cancelled during it cannot answer until
        it is done. That used to cost the whole of ``shutdown``'s timeout --
        "the app takes forever to close" -- because the wait loop could not
        tell "busy in native code" from "ignoring cancellation".

        This marks the window: ``shutdown`` skips a job that is inside one and
        lets process exit reap the daemon thread, which is what happened after
        the timeout anyway. It also checks the event on the way in and on the
        way out, so a cancel arriving either side of the load is honoured at
        once instead of waiting for the next loop checkpoint.

        It does not make the section interruptible. Nothing can.
        """
        self.raise_if_cancelled()
        self.job.uninterruptible = True
        self.job.current = what
        try:
            yield
        finally:
            self.job.uninterruptible = False
        self.raise_if_cancelled()


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
