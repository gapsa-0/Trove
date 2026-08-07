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
  spans two of them. The first two describe the archive that is currently
  open, and ``open_archive`` reloads them from it.
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
from . import archives, watcher
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
        # Pause state for the archive currently open -- both the whole-pipeline
        # switch and the per-stage set, which is separate from it and only
        # consulted while it is off (pausing one stage is about letting the
        # others keep going). They belong to the archive, not to the app:
        # open_archive loads the archive's own, and set_paused /
        # set_stage_paused write it back (cfg.archive_pause), so a pause is
        # remembered per archive across a restart and never follows the user to
        # a different one. Until an archive is open, the app-wide defaults
        # stand -- normally "not paused".
        self._paused, stages_off = cfg.archive_pause(None)
        self._paused_stages: set[str] = set(stages_off)
        # Work is deliberately opt-in per visible archive.  Starting the GUI
        # alone must not start touching an archive in the background.
        self._open_root_id: int | None = None
        # When a hint that files changed was last acted on, per root, and the
        # timer that will act on one that arrived too soon after it. See
        # note_files_changed for why a hint is throttled and never dropped.
        self._hint_at: dict[int, float] = {}
        self._hint_timer: dict[int, threading.Timer] = {}
        self._hint_lock = threading.Lock()
        # Public, because it is a collaborator rather than an implementation
        # detail: tests drive tick() by hand instead of waiting on the timer.
        self.scheduler = Scheduler(self)
        self.scheduler.start()
        # Started and stopped with the open archive, so only the archive being
        # looked at is watched -- the same rule the scheduler follows, which
        # also keeps the inotify watches held down to one tree.
        self._watcher = watcher.ArchiveWatcher(self.note_files_changed)
        # The root whose watch is owed but not yet placed, and the lock that
        # makes claiming it a single decision. open_archive records the debt
        # here rather than paying it on the spot; disk_count settles it once
        # the tree has been walked. See _watch_when_walked.
        self._watch_owed: tuple[int, str] | None = None
        self._watch_lock = threading.Lock()
        # Whether the one-shot pre-open warm has been kicked off. See warm_for_open.
        self._warmed = False
        self._warm_lock = threading.Lock()

    def shutdown(self, timeout: float = 8.0) -> bool:
        """Cancel all work and stop the scheduler before the HTTP server exits."""
        self.scheduler.stop()
        self._watcher.stop()
        # A hint deferred behind its floor has nothing left to wake: the
        # scheduler is stopping and note_files_changed would decline anyway.
        with self._hint_lock:
            for timer in self._hint_timer.values():
                timer.cancel()
            self._hint_timer.clear()
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

        Also where a deferred filesystem watch gets placed: a count came back,
        so this root's tree has been walked and the watch can be set up over
        warm metadata instead of cold. See ``_watch_when_walked``.
        """
        count = self._disk.count(root_id, root_path, max_age=max_age, allow_walk=allow_walk)
        if count is not None:
            self._place_owed_watch(root_id, root_path)
        return count

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

        Throttled per root, because acting on a hint costs a walk of the whole
        tree and files can arrive one at a time for as long as someone is
        dragging them in. A hint inside the floor is not dropped -- it is
        deferred to the end of it, so the last file of a slow trickle is still
        noticed without every file in the trickle costing a walk.
        """
        if self.scheduler.stopping():
            return
        with self._hint_lock:
            now = time.monotonic()
            last = self._hint_at.get(root_id)
            wait = 0.0 if last is None else watcher.WALK_FLOOR - (now - last)
            if wait > 0:
                if root_id not in self._hint_timer:
                    timer = threading.Timer(wait, self._fire_deferred_hint, args=(root_id,))
                    timer.daemon = True
                    self._hint_timer[root_id] = timer
                    timer.start()
                return
            self._hint_at[root_id] = now
        logger.debug("files changed under root=%s; re-checking", root_id)
        self._disk.invalidate(root_id)
        self.nudge()

    def _fire_deferred_hint(self, root_id: int) -> None:
        """The tail of a throttled hint, once its floor has passed."""
        with self._hint_lock:
            self._hint_timer.pop(root_id, None)
        self.note_files_changed(root_id)

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
        self._paused, stages_off = self.cfg.archive_pause(root_id)
        self._paused_stages = set(stages_off)
        path = self.cfg.archive_path(root_id)
        if path is not None:
            self._watch_when_walked(root_id, path)
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
        """The warm itself. Every failure here is swallowed: this has no result
        a caller is waiting for, so anything that goes wrong must cost only the
        speed it was trying to buy."""
        from pathlib import Path

        try:
            from ..faces import migrate_adaface  # noqa: F401  (imported for its cost)

            for entry in self.cfg.archives:
                if self.scheduler.stopping():
                    return
                db_path = self.cfg.archive_db_path(entry["id"])
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

    def _refresh_planner_stats(self, root_id: int) -> None:
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
            conn = db.connect(self.cfg.archive_db_path(root_id))
        except sqlite3.Error:
            return
        try:
            conn.execute("PRAGMA analysis_limit=50000")
            conn.execute("PRAGMA optimize=0x10002")
            conn.commit()
        except sqlite3.Error:
            logger.debug("could not refresh planner stats for root=%s", root_id, exc_info=True)
        finally:
            conn.close()

    def _watch_when_walked(self, root_id: int, path: str) -> None:
        """Owe this root a filesystem watch, to be placed after the next walk.

        Setting a recursive watch is not the cheap call it looks like. It walks
        the whole tree and stats every entry to find the directories it needs a
        watch on -- 151,310 ``statx`` calls for 595 watches on a 150k-file
        archive -- and ``watchfiles`` does that inside Rust, holding the GIL for
        its whole duration. Cold, that is one 1 KB metadata record per file off
        the disk: ~150 MB, and ~20 seconds on a spinning drive. Held GIL means
        those seconds are not slow, they are *stopped* -- no request of any kind
        is served while it runs, and opening an archive used to wait out all of
        it before the first screen could be drawn.

        So the watch is owed here and paid in ``disk_count``. The scheduler
        already walks this tree to decide what is pending, in Python, where
        every ``scandir`` releases the GIL and the app stays responsive
        throughout. Once that walk has been through, the same metadata is in the
        page cache and setting the watch over it costs ~0.3 s instead of ~20.

        Nothing is lost by waiting: this is a hint, and the poll behind it is
        what is actually correct -- see ``watcher``'s module docstring.
        """
        with self._watch_lock:
            self._watch_owed = (root_id, path)

    def _place_owed_watch(self, root_id: int, root_path: str) -> None:
        """Start the watch this root was owed, now that its tree is walked.

        Claimed under the lock so a burst of concurrent ``disk_count`` calls --
        the scheduler's tick and the status endpoint's snapshot routinely
        overlap -- places it exactly once.
        """
        with self._watch_lock:
            if self._watch_owed != (root_id, root_path):
                return
            self._watch_owed = None
        self._watcher.start(root_id, root_path)

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
        #
        # The debt goes first. A watch owed but not yet placed is settled by
        # whichever thread next finishes a walk, and a walk already in flight
        # can land after this returns -- forgetting to cancel here would start
        # watching an archive the user has just closed.
        with self._watch_lock:
            self._watch_owed = None
        self._watcher.stop()

    def _persist_pause(self) -> None:
        """Record the open archive's pause state, so reopening it restores it.

        Deliberately not fatal: the in-memory flags are what gate the scheduler
        and they are already set by the time this runs, so a disk hiccup must
        not leave a user who just asked to stop the CPU load still running. It
        is logged because the consequence is silent and surprising -- the pause
        is honoured now but forgotten on restart.

        Nothing to record while no archive is open: pause is a property of the
        archive (see cfg.archive_pause), and the GUI can only reach these
        controls from an open one anyway.
        """
        if self._open_root_id is None:
            return
        try:
            self.cfg.set_archive_pause(self._open_root_id, self._paused, self._paused_stages)
        except OSError:
            logger.warning("could not persist the pause state", exc_info=True)

    def set_paused(self, value: bool) -> None:
        """Toggle the whole-pipeline pause, for the archive that is open.

        The in-memory flag is what actually gates the scheduler and cancels
        running jobs, so it's set first and stays authoritative even if the
        config write fails (see _persist_pause).
        """
        self._paused = bool(value)
        logger.info("pipeline %s", "paused" if self._paused else "resumed")
        self._persist_pause()
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
        logger.info("stage %s %s", card, "paused" if value else "resumed")
        self._persist_pause()
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
                self._refresh_planner_stats(job.root_id)
            # React to completion at once: the next ready stage starts in
            # milliseconds instead of waiting out the idle poll interval.
            self.nudge()
