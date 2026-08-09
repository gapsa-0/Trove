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
* ``_error_at`` and the two switches inside ``_pause`` are read and written
  without ``_lock``. That is deliberate and safe here: each is a single atomic
  rebind or a ``dict``/``set`` mutation under the GIL, and no invariant spans
  two of them. The pause describes the archive that is currently open, and
  ``open_archive`` reloads it from that archive (see ``pausing``).
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

The one exception is a native call with no checkpoint inside it -- building an
ONNX session, which takes seconds. A runner wraps that in
``ctx.uninterruptible()``, and ``shutdown`` then declines to wait on it rather
than spending its whole timeout on a thread that cannot answer. That marker is
for genuinely un-cancellable code only; using it to excuse a loop that could
check the event would re-create the bug it exists to fix.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import nullcontext

from ..config import Config
from ..db import database as db
from . import archives, pausing, upkeep, watcher
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
        # Pause belongs to the archive, not to the app -- see pausing.py.
        self._pause = pausing.ArchivePause(cfg)
        # Work is deliberately opt-in per visible archive.  Starting the GUI
        # alone must not start touching an archive in the background.
        self._open_root_id: int | None = None
        # The housekeeping that is not jobs, each bit owning its own lock or
        # timer -- see upkeep.py for what they buy and why they are not part of
        # this module's threading contract.
        self._hints = upkeep.HintThrottle(self._act_on_hint, lambda: watcher.WALK_FLOOR)
        # Public, because it is a collaborator rather than an implementation
        # detail: tests drive tick() by hand instead of waiting on the timer.
        self.scheduler = Scheduler(self)
        self.scheduler.start()
        # Started and stopped with the open archive, so only the archive being
        # looked at is watched -- the same rule the scheduler follows, which
        # also keeps the inotify watches held down to one tree.
        self._watcher = watcher.ArchiveWatcher(self.note_files_changed)
        # Whether the one-shot pre-open warm has been kicked off. See warm_for_open.
        self._warmed = False
        self._warm_lock = threading.Lock()

    def shutdown(self, timeout: float = 8.0) -> bool:
        """Cancel all work and stop the scheduler before the HTTP server exits."""
        self.scheduler.stop()
        self._watcher.stop()
        # A hint deferred behind its floor has nothing left to wake: the
        # scheduler is stopping and note_files_changed would decline anyway.
        self._hints.cancel()
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
                # A job inside an uninterruptible section (loading an ONNX
                # model) is not waited for: it cannot answer until the native
                # call returns, and waiting out the full timeout only to kill
                # the daemon thread anyway is the "app takes forever to close"
                # bug. Skipping it reaches the same end state, sooner.
                waiting = [j for j in self._jobs.values() if j.status == "running"]
                blocked = [j.kind for j in waiting if j.uninterruptible]
                active = any(not j.uninterruptible for j in waiting)
            if not active:
                self.scheduler.join(timeout=max(0, deadline - time.monotonic()))
                if blocked:
                    logger.info(
                        "shutdown complete; %s left loading a model, which cannot be "
                        "interrupted -- its thread exits with the process",
                        ",".join(blocked),
                    )
                else:
                    logger.info("shutdown complete; no job left running")
                return True
            time.sleep(0.05)
        # A job that reaches here is one that had a checkpoint to reach and did
        # not reach it in time -- a runner not honouring its cancellation
        # contract, or a single step that is simply longer than the timeout.
        # Model loading no longer lands here; it is skipped above.
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
        return self._pause.paused

    def paused_stages(self) -> set[str]:
        return set(self._pause.stages)

    def stage_paused(self, card: str) -> bool:
        return self._pause.stage_paused(card)

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
        files_on_disk: int | None = None,
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
            job = Job(
                id=self._seq,
                kind=kind,
                root_id=root_id,
                root_path=root_path,
                force=force,
                files_on_disk=files_on_disk,
            )
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
    def _open_db(self, root_id: int) -> sqlite3.Connection:
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

    def note_files_changed(self, root_id: int) -> None:
        """Something has changed on disk under this root; find out sooner.

        The one entry point for every hint, whichever direction it came from --
        the filesystem watcher, or the window regaining focus after someone
        dropped files in. Neither is trusted to say *what* changed: both do the
        same two things, drop the cached disk count and wake the scheduler, and
        the tick that follows re-walks and decides. That is why a hint being
        wrong, duplicated or absent costs nothing but timing.

        Throttled per root -- see ``upkeep.HintThrottle`` for why a hint is
        deferred rather than dropped.
        """
        if self.scheduler.stopping():
            return
        self._hints.note(root_id, time.monotonic())

    def _act_on_hint(self, root_id: int) -> None:
        """What a hint does once the throttle lets it through."""
        logger.debug("files changed under root=%s; re-checking", root_id)
        self._disk.invalidate(root_id)
        self.nudge()

    def open_archive(self, root_id: int) -> None:
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
        # Before the nudge, so a nudge cannot start work this archive is paused
        # on. This is also what stops a pause from following the user: whatever
        # the *previous* archive was left on is replaced by this one's own state.
        self._pause.load(root_id)
        path = self.cfg.archive_path(root_id)
        if path is not None:
            # Placed here rather than deferred until the first walk: setting it
            # up costs ~0.4 s on its own thread now (see ``watcher``), so there
            # is nothing left to wait for -- and waiting meant files dropped in
            # during that window were noticed only by the poll behind it.
            self._watcher.start(root_id, path)
        self.nudge()

    def warm_for_open(self) -> None:
        """Pull what opening an archive is about to need into cache, in the background.

        Called when the picker is served, which is the one idle moment this
        process reliably gets: the list is on screen and the user is seconds
        from clicking a card. Opening an archive is only ~175 ms of *work*, but
        on a cold cache it is several seconds of small reads to reach it -- the
        ``trove.faces`` import ``_open_db`` defers is 845 ms by itself (124 ms
        once loaded), and the schema check behind it walks every archive's
        ``sqlite_master``.

        Read-only and idempotent by construction: it opens nothing for writing
        and calls nothing that migrates. ``_open_db`` still does all of that for
        real when the archive is opened -- the only difference is that it finds
        the pages already in memory when it does.

        Once per process. The picker is redrawn whenever an archive is added or
        removed, and there is nothing to warm a second time.
        """
        with self._warm_lock:
            if self._warmed:
                return
            self._warmed = True
        threading.Thread(target=self._warm, name="warm-open", daemon=True).start()

    def _warm(self) -> None:
        """The warm itself, over every archive that has a database yet."""
        upkeep.warm_archives(
            lambda: [self.cfg.archive_db_path(e["id"]) for e in self.cfg.archives],
            self.scheduler.stopping,
        )

    def close_archive(self, root_id: int | None = None) -> None:
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
        # Outside the registry lock, and only once this really was the open
        # archive being closed: a mismatched root_id returns above without
        # touching anything, and stopping the watch there would leave the
        # archive that is still open unwatched.
        self._watcher.stop()

    def set_paused(self, value: bool) -> None:
        """Toggle the whole-pipeline pause, for the archive that is open.

        The in-memory flag is what actually gates the scheduler and cancels
        running jobs, so it is set first and stays authoritative even if the
        config write fails (see ``pausing``).
        """
        if self._pause.set_paused(value):
            # This is what actually stops the CPU load; jobs resume from
            # their last committed batch, same mechanism as close_archive.
            self._cancel_running()
        else:
            self.nudge()

    def set_stage_paused(self, card: str, value: bool) -> None:
        """Pause/resume ONE stage card, leaving every other stage running.

        Same mechanism as the whole-pipeline pause, scoped to the kinds that
        card represents (``stages.kinds_of``): pausing cancels that stage's
        running job at its next batch checkpoint so the CPU actually frees up
        instead of only the *next* run being skipped.
        """
        from . import stages

        self._pause.set_stage(card, value)
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

    def nudge(self) -> None:
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

        def context(conn: sqlite3.Connection | None) -> JobContext:
            return JobContext(cfg=self.cfg, job=job, cancel=cancel, conn=conn, log=logger)

        if not runner.needs_connection:
            return runner.run(context(None))
        # The connection is opened *inside* the write lock for the stages that
        # take it: _open_db can itself write (schema migration, root
        # reconciliation), so opening it outside would put a writer ahead of
        # the lock that exists to order writers.
        with self._write_lock if runner.takes_write_lock else nullcontext():
            # Job.root_id is optional on the dataclass (JobManager.start's
            # signature allows a rootless job), but a runner that wants a
            # connection needs a real archive to open. require_root raises
            # rather than fabricating one -- see its docstring.
            root_id = job.require_root()
            conn = self._open_db(root_id)
            try:
                runner.run(context(conn))
            finally:
                conn.close()

    def _run(self, job: Job, cancel: threading.Event) -> None:
        try:
            runner = RUNNERS.get(job.kind)
            if runner is None:
                raise ValueError(f"unknown job kind: {job.kind}")
            self._dispatch(runner, job, cancel)
            job.status = "done"
            # A successful rebuild is the only thing that marks dedup_runs (see
            # runners/dedup.py), so a cancelled or errored dedup leaves no
            # marker and stays queued/retries next tick.
            # Skipped rather than required for a rootless job: the cooldown is
            # per (archive, stage), so there is nothing to clear for a job that
            # belongs to no archive.
            if job.root_id is not None:
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
            if job.root_id is not None:
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
            if job.kind == "scan" and job.root_id is not None:
                self._disk.invalidate(job.root_id)
            # A stage that just rewrote a table changed the shape of what the
            # planner has to reason about, and this is the moment to tell it.
            if job.root_id is not None:
                upkeep.refresh_planner_stats(self.cfg.archive_db_path(job.root_id))
            # React to completion at once: the next ready stage starts in
            # milliseconds instead of waiting out the idle poll interval.
            self.nudge()
