"""In-process background task runner for the GUI.

Runs long operations (scan, enrich) in worker threads and exposes their live
progress for polling. Write-tasks are serialized by a global lock so only one
touches the SQLite writer at a time (reads via the GUI stay concurrent).

Threading contract
------------------
This module and ``scheduler.py`` are the only two that know about threads --
this one owns the workers, that one owns the single polling thread -- and these
are the rules they keep. They were true before they were written down; the
point of writing them down is that the next change here can be checked against
them.

**Threads.** Three kinds touch a ``JobManager``: the HTTP threads (many, one
per request), the scheduler thread (exactly one, created by the ``Scheduler``
this manager constructs and stopped by ``shutdown``), and one worker thread per
running job.

**Who may start a job.** Only the scheduler thread, from ``scheduler.tick()``. An
HTTP thread that wants work to begin calls ``nudge()``, which sets an event and
returns; it never starts a job itself. Two mutations do call ``start()``
directly (a re-cluster after an unmerge), and those are the exception that
proves the rule -- they are one-shot jobs with no dependencies.

**Locks.**

* ``_lock`` guards the job registry: ``_jobs``, ``_cancels`` and ``_seq``.
  Hold it briefly; never hold it while running a job or opening a database.
* ``_write_lock`` serialises the stages that rewrite tables wholesale, so at
  most one of them holds SQLite's writer at a time. A runner declares whether
  it needs this (``Runner.takes_write_lock``); the manager, not the runner,
  takes it.
* ``_paused``, ``_paused_stages`` and ``_error_at`` are plain attributes read
  and written without ``_lock``. That is deliberate and safe here: each is a
  single atomic rebind or a ``dict`` mutation under the GIL, and no invariant
  spans two of them.
* The disk-count cache has moved to ``archives.DiskCounts``, which owns a lock
  of its own -- it never needed ordering against the job registry.

**Connections.** A runner gets its own connection and must not share it with
another thread -- SQLite connections are not safe across threads by default.
The manager opens it, hands it over as ``ctx.conn``, and closes it afterwards.

**Cancellation.** Every job has a ``threading.Event``. A runner **must** check
it at each loop iteration -- via ``ctx.raise_if_cancelled()`` or by calling
``ctx.progress().update()``, which raises on a set event -- and return
promptly. A runner that ignores it makes the app un-quittable, because
shutdown waits on the worker threads. Runners must also commit incrementally,
so an interrupted job leaves usable partial progress rather than nothing.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import nullcontext

from ..config import Config
from ..db import database as db
from . import archives
from .job import Job, JobContext, Runner
from .runners import RUNNERS
from .scheduler import Scheduler

logger = logging.getLogger(__name__)

# Re-exported for the tests and callers that build a Job directly; the type
# itself lives in job.py so that runners/ can use it without importing this
# module, which imports them.
__all__ = ["Job", "JobManager"]


class JobManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._jobs: dict[int, Job] = {}
        self._cancels: dict[int, threading.Event] = {}
        self._seq = 0
        self._lock = threading.Lock()  # guards registry
        self._write_lock = threading.Lock()  # serializes DB writers

        # Dedup rebuilds every group wholesale (no per-file backlog), so its
        # "needs a run" signal is catalog-derived (db.dedup_needed /
        # dedup_runs) rather than an in-memory flag -- see dedup_needed()
        # below. That table is what survives a restart; nothing about dedup
        # readiness lives on this object.
        # Disk-walk cache per root: counting files on disk is the one expensive
        # part of freshness, so the status endpoint (polled ~1/s) reads a cached
        # count instead of walking ~150k files every time.
        self._disk = archives.DiskCounts(lambda: self.scheduler.stopping())
        # When a stage's job errors, hold off auto-restarting that kind for a
        # cooldown so a hard failure can't hot-loop through the nudge path.
        self._error_at: dict[tuple[int, str], float] = {}
        # Whole-pipeline pause (things_to_fix #19): one global flag, not
        # per-archive -- only one archive is ever open at a time (see
        # current_root_id). Seeded from the persisted config so a pause
        # survives an app restart.
        self._paused = bool(getattr(cfg, "pipeline_paused", False))
        # Per-stage pause, by display card id. Separate from the global flag
        # above and only consulted while it is off (things_to_fix #32: pausing
        # one stage is about letting the others keep going). Persisted the same
        # way, so a stage the user stopped stays stopped across a restart.
        self._paused_stages: set[str] = set(getattr(cfg, "paused_stages", None) or [])
        # Work is deliberately opt-in per visible archive.  Starting the GUI
        # alone must not start touching an archive in the background.
        self._open_root_id: int | None = None
        # Public, because it is a collaborator rather than an implementation
        # detail: tests drive tick() by hand instead of waiting on the timer.
        self.scheduler = Scheduler(self)
        self.scheduler.start()

    def shutdown(self, timeout: float = 8.0) -> bool:
        """Cancel all work and stop the scheduler before the HTTP server exits."""
        self.scheduler.stop()
        with self._lock:
            # Set every cancel directly rather than going through
            # _cancel_running: on the way out, jobs of every kind and root stop.
            running = [(j.kind, j.root_id) for j in self._jobs.values() if j.status == "running"]
            for cancel in self._cancels.values():
                cancel.set()
        logger.info(
            "shutdown requested; cancelling %d running job(s): %s",
            len(running),
            ",".join(f"{kind}(root={root})" for kind, root in running) or "-",
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                active = any(job.status == "running" for job in self._jobs.values())
            if not active:
                self.scheduler.join(timeout=max(0, deadline - time.monotonic()))
                logger.info("shutdown complete; no job left running")
                return True
            time.sleep(0.05)
        # "The app takes forever to close" ends up here. A job only yields at a
        # progress checkpoint, and the stages that load a model spend their first
        # seconds with no checkpoint to reach -- so the timeout is survivable, but
        # it must not be silent, and the kinds above say which stage was slow.
        with self._lock:
            stragglers = [j.kind for j in self._jobs.values() if j.status == "running"]
        logger.warning(
            "shutdown timed out after %.1fs; still running: %s",
            timeout,
            ",".join(stragglers) or "-",
        )
        return False

    # -- introspection ----------------------------------------------------
    def list(self, root_id: int | None = None) -> list[dict]:
        with self._lock:
            js = sorted(self._jobs.values(), key=lambda j: j.id, reverse=True)
        return [j.public() for j in js if root_id is None or j.root_id == root_id]

    def active_kind(self, kind: str) -> bool:
        return any(j.status == "running" and j.kind == kind for j in self._jobs.values())

    def disk_count(
        self, root_id: int, root_path: str, max_age: float | None = None, allow_walk: bool = True
    ) -> int | None:
        """Files on disk under this root. See ``archives.DiskCounts.count``.

        Kept on the manager because ``stages._pending`` reads it off whatever
        object it is handed, and that object is this one.
        """
        return self._disk.count(root_id, root_path, max_age=max_age, allow_walk=allow_walk)

    def dedup_needed(self, root_id: int) -> bool:
        """Whether a duplicate rebuild is outstanding. See ``archives``.

        Kept on the manager for the same reason as ``disk_count``.
        """
        return archives.dedup_needed(self.cfg, root_id)

    def current_root_id(self) -> int | None:
        """Which single archive is open in the GUI right now, if any.

        Each archive is a separate database, so content routes that carry no
        root/archive id of their own (thumbnails, the original file) resolve
        against whichever one this is — the GUI only ever browses one at a time.
        """
        return self._open_root_id

    def paused(self) -> bool:
        return self._paused

    def paused_stages(self) -> set[str]:
        return set(self._paused_stages)

    def stage_paused(self, card: str) -> bool:
        return card in self._paused_stages

    def stop_archive(self, root_id: int, timeout: float = 10.0) -> bool:
        """Cancel this archive's work and wait briefly for safe DB quiescence.

        Deleting rows while a scanner or metadata worker is still committing
        would let it recreate part of an archive after removal. Jobs observe
        cancellation at their normal batch checkpoints, so this usually returns
        immediately; callers can retry if a long external operation is winding
        down.
        """
        with self._lock:
            # This can include a job that was already being cancelled after the
            # user switched archives. It still needs an explicit signal here:
            # removal must never proceed until every worker for this root exits.
            for jid, job in self._jobs.items():
                if job.status == "running" and job.root_id == root_id:
                    self._cancels[jid].set()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                active = any(
                    j.status == "running" and j.root_id == root_id for j in self._jobs.values()
                )
            if not active:
                return True
            time.sleep(0.05)
        with self._lock:
            return not any(
                j.status == "running" and j.root_id == root_id for j in self._jobs.values()
            )

    # -- control ----------------------------------------------------------
    def start(
        self,
        kind: str,
        root_id: int | None = None,
        root_path: str | None = None,
        force: bool = False,
    ) -> dict:
        if self.scheduler.stopping():
            return {"error": "application is shutting down"}
        # All GUI jobs belong to the archive currently on screen.  This also
        # closes the small race where the user switches archives between a
        # scheduler decision and this call.
        if root_id is not None and root_id != self._open_root_id:
            return {"error": "archive is no longer open"}
        if self.active_kind(kind):
            return {"error": f"a {kind} job is already running"}
        with self._lock:
            self._seq += 1
            job = Job(id=self._seq, kind=kind, root_id=root_id, root_path=root_path, force=force)
            self._jobs[job.id] = job
            cancel = threading.Event()
            self._cancels[job.id] = cancel
        logger.info("job start kind=%s root=%s job=%s force=%s", kind, root_id, job.id, force)
        t = threading.Thread(target=self._run, args=(job, cancel), daemon=True)
        t.start()
        return job.public()

    # -- automatic scheduling ----------------------------------------------
    # While an archive is open, this daemon notices pending work and runs the
    # pipeline (scan → enrich → dedup → pets → faces) for it. Closing or switching the
    # archive requests cancellation; long jobs commit in batches and resume the
    # next time that archive is opened.
    def _open_db(self, root_id: int):
        """Connect to an archive, bringing schema and root identity up to date.

        The schema part matters because the read-only query paths (thumbnails,
        the viewer) join tables this version expects, and an archive catalogued
        by an older one would not have them yet. The root part matters because
        every query, job and URL in the GUI addresses this archive by its
        registry id, so the database's single root has to *be* that id — see
        db.reconcile_root for what goes wrong when it isn't.
        """
        conn = db.connect(self.cfg.archive_db_path(root_id))
        try:
            db.init_db(conn)
            path = self.cfg.archive_path(root_id)
            if path and db.reconcile_root(conn, root_id, path):
                # The file set just changed shape; whatever dedup last covered
                # no longer describes it.
                db.dedup_invalidate(conn, root_id)
                conn.commit()
            # If this archive's face vectors came from a different embedder they
            # are unusable, so clear them and the scan markers here: the DETECT
            # backlog is derived from `face_scan`, so the ordinary pipeline picks
            # the work up on its own and refills from zero. Cheap no-op (one
            # primary-key lookup) once the archive is on the current embedder.
            from ..faces import migrate_adaface

            migrate_adaface.run_if_needed(conn, self.cfg, db_path=self.cfg.archive_db_path(root_id))
        except Exception:
            # Close then re-raise: not silent, and _run logs it with the job it
            # belongs to. Logging here as well would record one failure twice.
            conn.close()
            raise
        return conn

    def open_archive(self, root_id: int):
        """Allow automatic work for this archive while it is being viewed."""
        self._open_db(root_id).close()
        with self._lock:
            previous = self._open_root_id
            self._open_root_id = root_id
            # Changing archives means the old archive is no longer open.  Its
            # resumable job should yield at its next progress checkpoint.
            if previous is not None and previous != root_id:
                for jid, job in self._jobs.items():
                    if job.status == "running" and job.root_id == previous:
                        self._cancels[jid].set()
        self.nudge()

    def close_archive(self, root_id: int | None = None):
        """Stop work when the currently viewed archive is closed."""
        with self._lock:
            if self._open_root_id is None or (
                root_id is not None and root_id != self._open_root_id
            ):
                return
            closing = self._open_root_id
            self._open_root_id = None
            for jid, job in self._jobs.items():
                if job.status == "running" and job.root_id == closing:
                    self._cancels[jid].set()

    def set_paused(self, value: bool) -> None:
        """Toggle the whole-pipeline pause.

        The in-memory flag is what actually gates the scheduler and cancels
        running jobs, so it's set first and stays authoritative even if the
        config write below fails -- a disk hiccup must not silently leave a
        user who just asked to stop the CPU load still running.
        """
        self._paused = bool(value)
        self.cfg.pipeline_paused = self._paused
        logger.info("pipeline %s", "paused" if self._paused else "resumed")
        try:
            self.cfg.save()
        except OSError:
            # Deliberately not fatal, per the docstring: the in-memory flag is
            # authoritative. Recorded because the consequence is silent and
            # surprising -- the pause is honoured now but forgotten on restart.
            logger.warning("could not persist the pipeline pause state", exc_info=True)
        if self._paused:
            # This is what actually stops the CPU load; jobs resume from
            # their last committed batch, same mechanism as close_archive.
            self._cancel_running()
        else:
            self.nudge()

    def set_stage_paused(self, card: str, value: bool) -> None:
        """Pause/resume ONE stage card, leaving every other stage running.

        Same mechanism as the whole-pipeline pause, scoped to the kinds that
        card represents (``stages.kinds_of``): the in-memory set gates the
        scheduler and is authoritative even if persisting it fails, and pausing
        cancels that stage's running job at its next batch checkpoint so the
        CPU actually frees up instead of only the *next* run being skipped.
        """
        from . import stages

        if value:
            self._paused_stages.add(card)
        else:
            self._paused_stages.discard(card)
        self.cfg.paused_stages = sorted(self._paused_stages)
        logger.info("stage %s %s", card, "paused" if value else "resumed")
        try:
            self.cfg.save()
        except OSError:
            # Same reasoning as set_paused: honoured now, forgotten on restart.
            logger.warning("could not persist the paused-stage set", exc_info=True)
        if value:
            self._cancel_running(stages.kinds_of(card))
        else:
            self.nudge()

    def _cancel_running(self, kinds: frozenset[str] | None = None) -> None:
        """Ask running jobs (of these kinds, or all) to stop at their next
        checkpoint. Progress is committed in batches, so this loses no work."""
        with self._lock:
            for jid, job in self._jobs.items():
                if job.status == "running" and (kinds is None or job.kind in kinds):
                    logger.info(
                        "job cancel requested kind=%s root=%s job=%s", job.kind, job.root_id, jid
                    )
                    self._cancels[jid].set()

    def nudge(self):
        """Wake the scheduler now after an archive has been opened.

        Kept on the manager because ``_run``'s ``finally`` and several HTTP
        paths call it; it is the one scheduler operation the rest of the app
        asks for by name."""
        self.scheduler.nudge()

    # -- worker -----------------------------------------------------------
    def _dispatch(self, runner: Runner, job: Job, cancel: threading.Event) -> None:
        """Set a runner up the way it declared it needs, then call it.

        The two flags on ``Runner`` are read here and nowhere else, so "which
        stages serialise against each other" is answerable by reading the
        registry rather than by reading every runner's body.
        """

        def context(conn):
            return JobContext(cfg=self.cfg, job=job, cancel=cancel, conn=conn, log=logger)

        if not runner.needs_connection:
            return runner.run(context(None))
        # The connection is opened *inside* the write lock for the stages that
        # take it: _open_db can itself write (schema migration, root
        # reconciliation), so opening it outside would put a writer ahead of
        # the lock that exists to order writers.
        with self._write_lock if runner.takes_write_lock else nullcontext():
            conn = self._open_db(job.root_id)
            try:
                runner.run(context(conn))
            finally:
                conn.close()

    def _run(self, job: Job, cancel: threading.Event):
        try:
            runner = RUNNERS.get(job.kind)
            if runner is None:
                raise ValueError(f"unknown job kind: {job.kind}")
            self._dispatch(runner, job, cancel)
            job.status = "done"
            # A successful rebuild is the only thing that marks dedup_runs (see
            # runners/dedup.py), so a cancelled or errored dedup leaves no
            # marker and stays queued/retries next tick.
            self._error_at.pop((job.root_id, job.kind), None)
            logger.info(
                "job done kind=%s root=%s job=%s done=%s total=%s elapsed=%.1fs %s",
                job.kind,
                job.root_id,
                job.id,
                job.done,
                job.total,
                time.time() - job.started_at,
                job.message,
            )
        except KeyboardInterrupt:
            job.status = "cancelled"
            job.message = "cancelled; progress saved"
            # Not a failure: this is the pause/close/switch-archive path, which
            # cancels at a batch checkpoint. Recorded so a log that ends mid-run
            # can be told apart from one where the job was stopped on purpose.
            logger.info(
                "job cancelled kind=%s root=%s job=%s done=%s total=%s elapsed=%.1fs",
                job.kind,
                job.root_id,
                job.id,
                job.done,
                job.total,
                time.time() - job.started_at,
            )
        except Exception as e:
            job.status = "error"
            job.message = f"{e}"
            self._error_at[(job.root_id, job.kind)] = time.monotonic()
            logger.error(
                "job failed kind=%s root=%s job=%s after=%.1fs",
                job.kind,
                job.root_id,
                job.id,
                time.time() - job.started_at,
                exc_info=True,
            )
        finally:
            job.finished_at = time.time()
            # A finished scan changed what's on disk-vs-indexed; drop the cached
            # walk so freshness reflects it immediately.
            if job.kind == "scan":
                self._disk.invalidate(job.root_id)
            # React to completion at once: the next ready stage starts in
            # milliseconds instead of waiting out the idle poll interval.
            self.nudge()
