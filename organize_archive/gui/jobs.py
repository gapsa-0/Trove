"""In-process background task runner for the GUI.

Runs long operations (scan, enrich) in worker threads and exposes their live
progress for polling. Write-tasks are serialized by a global lock so only one
touches the SQLite writer at a time (reads via the GUI stay concurrent).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field

from ..config import Config
from ..db import database as db
from ..scan import walker

logger = logging.getLogger(__name__)


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


class _JobProgress:
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


class JobManager:
    # Idle poll interval backs off (up to _AUTO_MAX) when a tick finds nothing
    # to do, so a quiet archive doesn't get walked every few seconds forever.
    _AUTO_MIN = 10
    _AUTO_MAX = 300
    # Images per detect-then-recluster chunk in the detect job (see _run_detect):
    # small enough that people/pets appear early in a multi-hour run, large enough
    # that repeated clustering stays a small fraction of total time. Detection is
    # heavier per image than one detector was (SCRFD + YOLOX + embeds), so the
    # chunk is smaller than the old faces chunk.
    _DETECT_CHUNK = 600
    # Freshness disk walk is reused for this long before re-walking (see
    # _walk_cache). Comfortably below the idle backoff so an idle tick still
    # re-walks and notices files added to the source folder.
    _WALK_TTL = 60.0
    # After a job of some kind errors, wait this long before auto-restarting the
    # same kind, so a persistent failure backs off instead of spinning.
    _ERROR_COOLDOWN = 120.0

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
        # count instead of walking ~150k files every time. (monotonic_ts, count)
        self._walk_cache: dict[int, tuple[float, int]] = {}
        # Roots with a background refresh walk in flight (see disk_count).
        self._walking: set[int] = set()
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
        self._auto_interval = self._AUTO_MIN
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._scheduler = threading.Thread(target=self._auto_loop, daemon=True)
        self._scheduler.start()

    def shutdown(self, timeout: float = 8.0) -> bool:
        """Cancel all work and stop the scheduler before the HTTP server exits."""
        self._stopping.set()
        self._wake.set()
        with self._lock:
            for cancel in self._cancels.values():
                cancel.set()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                active = any(job.status == "running" for job in self._jobs.values())
            if not active:
                self._scheduler.join(timeout=max(0, deadline - time.monotonic()))
                return True
            time.sleep(0.05)
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
        """Files on disk under this root, cached so the polled status endpoint
        never triggers a fresh ~150k-file walk. Returns None if the folder is
        gone.

        ``allow_walk=False`` additionally refuses to *wait* for a refresh walk,
        which is what the status endpoint passes. Counting 150k files on a busy
        external drive can take longer than the client's poll interval, and a
        walk on the request thread then stacks requests up against the browser's
        handful of connections until the whole UI stops updating. The stale
        count is served instead and a refresh runs in the background. The very
        first walk still blocks: there is nothing to serve yet, and answering
        "no idea" would show every stage as up to date on the first paint.
        """
        from pathlib import Path

        from ..scan.walker import count_files

        ttl = self._WALK_TTL if max_age is None else max_age
        hit = self._walk_cache.get(root_id)
        now = time.monotonic()
        if hit and now - hit[0] < ttl:
            return hit[1]
        if hit and not allow_walk:
            self._refresh_disk_count(root_id, root_path)
            return hit[1]
        if not Path(root_path).is_dir():
            self._walk_cache.pop(root_id, None)
            return None
        count = count_files(Path(root_path))
        self._walk_cache[root_id] = (now, count)
        return count

    def _refresh_disk_count(self, root_id: int, root_path: str) -> None:
        """Re-walk this root off the caller's thread, one walk at a time."""
        with self._lock:
            if root_id in self._walking or self._stopping.is_set():
                return
            self._walking.add(root_id)

        def run():
            try:
                self.disk_count(root_id, root_path, max_age=0)
            except OSError:
                pass
            finally:
                with self._lock:
                    self._walking.discard(root_id)

        threading.Thread(target=run, daemon=True).start()

    def dedup_needed(self, root_id: int) -> bool:
        """Whether a duplicate rebuild is outstanding for this root.

        Derived entirely from the catalog (db.dedup_needed / dedup_runs):
        a freshly constructed JobManager -- e.g. right after an app restart --
        gives the same answer a long-running one would have, instead of the
        old in-memory flag's default-dirty-on-every-start behaviour.
        """
        conn = db.open_readonly(self.cfg.archive_db_path(root_id))
        try:
            return db.dedup_needed(conn, root_id)
        finally:
            conn.close()

    def _mark_dedup_owed(self, root_id: int) -> None:
        """Persist that a dedup rebuild is owed for this root.

        Scan can change the file set and enrich can populate metadata dedup's
        canonical-selection rule reads (Takeout sidecar, resolved date,
        dimensions) without changing the count/max id of present, hashed
        files `dedup_needed` otherwise compares (enrich never touches
        `files.sha256`/`present`) -- so that comparison alone would miss a
        case where the previous grouping's canonical pick is now stale.
        Invalidating through the same persisted marker `_run_dedup` writes on
        success means this obligation survives a restart too.

        Runs on the scheduler thread, which must not die on a transient lock:
        this tiny write can land while a detect batch holds the writer, and an
        escaping OperationalError would abort the whole tick before any stage
        starts. A lost invalidation is harmless — the next tick re-derives the
        same obligation and tries again.
        """

        def _write():
            conn = db.connect(self.cfg.archive_db_path(root_id))
            try:
                db.dedup_invalidate(conn, root_id)
                conn.commit()
            finally:
                conn.close()

        try:
            db.write_with_retry(_write)
        except sqlite3.OperationalError:
            pass

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

    def _error_ready(self, root_id: int, kind: str) -> bool:
        """True once a kind's post-error cooldown has elapsed (or none is set)."""
        at = self._error_at.get((root_id, kind))
        return at is None or (time.monotonic() - at) >= self._ERROR_COOLDOWN

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
        if self._stopping.is_set():
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
        card represents (``pipeline.kinds_of``): the in-memory set gates the
        scheduler and is authoritative even if persisting it fails, and pausing
        cancels that stage's running job at its next batch checkpoint so the
        CPU actually frees up instead of only the *next* run being skipped.
        """
        from . import pipeline

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
            self._cancel_running(pipeline.kinds_of(card))
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
        """Wake the scheduler now after an archive has been opened."""
        self._auto_interval = self._AUTO_MIN
        self._wake.set()

    def _auto_loop(self):
        while not self._stopping.is_set():
            self._wake.wait(self._auto_interval)
            self._wake.clear()
            if self._stopping.is_set():
                break
            try:
                acted = self._auto_tick()
            except Exception:
                # The scheduler thread must outlive any single bad tick, so this
                # stays broad -- but an unlogged one made the whole pipeline look
                # merely idle.
                logger.exception("scheduler tick failed")
                acted = False
            self._auto_interval = (
                self._AUTO_MIN if acted else min(self._auto_interval * 1.5, self._AUTO_MAX)
            )

    def _auto_tick(self) -> bool:
        """One scheduling decision, driven entirely by the same pipeline snapshot
        the GUI renders. Starts every stage that is ready to run and returns True
        while any work is outstanding, so the loop keeps the fast interval until
        the pipeline is fully idle.

        Readiness (deps satisfied, backend available, not already running) is
        already resolved in the snapshot: a stage is ``queued`` exactly when it
        should start now. Parallel kinds (scan ∥ enrich ∥ semantic) start
        together; the DB-writer stages (dedup → places/pets → faces) start one at
        a time, matching the single write lock.
        """
        if self._paused:
            logger.debug("tick: skipped, pipeline is paused")
            return False
        from . import pipeline, queries

        open_root_id = self._open_root_id
        if open_root_id is None:
            logger.debug("tick: skipped, no archive is open")
            return False
        archive = next(
            (a for a in queries.archives(self.cfg) if a["id"] == open_root_id and a["exists"]), None
        )
        if not archive:
            logger.debug("tick: skipped, archive root=%s is missing or unregistered", open_root_id)
            return False

        states = pipeline.stage_states(
            self.cfg, self, open_root_id, archive["path"], allow_walk=True
        )
        stalled = self._stalled_kinds(states)
        lock_running = any(
            s["kind"] in pipeline.LOCK_KINDS and s["state"] == "running" for s in states
        )
        acted = False
        started_lock = False
        for s in states:
            kind, state = s["kind"], s["state"]
            if state not in ("queued", "error"):
                continue
            # A stage the user paused on its own is simply never started; its
            # siblings are untouched, which is the whole point of #32.
            if self.stage_paused(pipeline.card_of(kind)):
                continue
            if state == "error" and not self._error_ready(open_root_id, kind):
                continue
            if kind in pipeline.PARALLEL_KINDS:
                if self.active_kind(kind):
                    continue
            else:  # single-writer stage: at most one at a time
                if lock_running or started_lock:
                    continue
            # Scanning or enriching may change the file set, so a fresh duplicate
            # rebuild is owed once they finish.
            if kind in (pipeline.SCAN, pipeline.ENRICH):
                self._mark_dedup_owed(open_root_id)
            # dedup/places operate per-root via root_id and ignore root_path.
            path = None if kind in (pipeline.DEDUP, pipeline.PLACES) else archive["path"]
            if "error" not in self.start(kind, open_root_id, path):
                acted = True
                if kind in pipeline.LOCK_KINDS:
                    started_lock = True
        # Keep polling promptly while anything is running or waiting to run.
        # Work held back by a per-stage pause does not count -- neither the
        # paused stage nor anything queued behind it can start until the user
        # says so, and counting it would pin the scheduler (and its disk walk)
        # to the fast interval indefinitely.
        outstanding = any(
            s["state"] in ("running", "queued", "blocked", "error") and not stalled[s["kind"]]
            for s in states
        )
        # One line per tick, listing what the scheduler saw and what it did with
        # it. "Why did the next stage not start?" is otherwise unanswerable: a
        # tick that starts nothing looks identical to no tick at all. Guarded by
        # isEnabledFor because this runs every 10s and the join is not free.
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "tick: root=%s acted=%s outstanding=%s states=%s stalled=%s",
                open_root_id,
                acted,
                outstanding,
                ",".join(f"{s['kind']}={s['state']}" for s in states),
                ",".join(sorted(k for k, v in stalled.items() if v)) or "-",
            )
        return acted or outstanding

    def _stalled_kinds(self, states: list[dict]) -> dict[str, bool]:
        """Which stages cannot progress because of a per-stage pause: the paused
        ones, plus everything waiting on a stage that is itself stalled.

        ``states`` is in dependency order (pipeline.STAGES), so one forward pass
        propagates the whole chain -- pausing Deduplication also stops the map,
        detection and semantic stages that queue behind it.
        """
        from . import pipeline

        stalled: dict[str, bool] = {}
        for s in states:
            blocker = s["blocker"]
            stalled[s["kind"]] = self.stage_paused(pipeline.card_of(s["kind"])) or bool(
                blocker and stalled.get(blocker)
            )
        return stalled

    # -- worker -----------------------------------------------------------
    def _run(self, job: Job, cancel: threading.Event):
        try:
            if job.kind == "semantic":
                self._run_semantic(job, cancel)
            elif job.kind in ("scan", "enrich"):
                # Scan and enrich each use their own connection and bypass the
                # write lock so they can run in parallel. SQLite WAL mode
                # serializes their brief commits safely.
                conn = self._open_db(job.root_id)
                try:
                    if job.kind == "scan":
                        self._run_scan(conn, job, cancel)
                    else:
                        self._run_enrich(conn, job, cancel)
                finally:
                    conn.close()
            else:
                with self._write_lock:
                    conn = self._open_db(job.root_id)
                    try:
                        if job.kind == "dedup":
                            self._run_dedup(conn, job, cancel)
                        elif job.kind == "places":
                            self._run_places(conn, job, cancel)
                        elif job.kind == "detect":
                            self._run_detect(conn, job, cancel)
                        elif job.kind == "face_cluster":
                            self._run_face_cluster(conn, job, cancel)
                        elif job.kind == "pet_cluster":
                            self._run_pet_cluster(conn, job, cancel)
                        else:
                            raise ValueError(f"unknown job kind: {job.kind}")
                    finally:
                        conn.close()
            job.status = "done"
            # A successful rebuild is the only thing that marks dedup_runs (see
            # _run_dedup), so a cancelled or errored dedup leaves no marker and
            # stays queued/retries next tick.
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
                self._walk_cache.pop(job.root_id, None)
            # React to completion at once: the next ready stage starts in
            # milliseconds instead of waiting out the idle poll interval.
            self.nudge()

    def _run_scan(self, conn, job: Job, cancel):
        from pathlib import Path

        prog = _JobProgress(job, cancel)
        run_started = db.now_iso()
        # An archive database has exactly one root; job.root_path is always
        # supplied by the scheduler, this is just a defensive fallback.
        roots = [job.root_path] if job.root_path else [self.cfg.archive_path(job.root_id)]
        on_disk = sum(walker.count_files(Path(r)) for r in roots if Path(r).is_dir())
        prog.total = on_disk
        run_id = db.scan_run_start(conn, job.root_id, roots)
        totals = walker.ScanStats()
        for r in roots:
            stats = walker.scan_root(
                conn,
                self.cfg,
                r,
                run_started,
                progress=prog,
                base_done=totals.seen,
                # Small batches so the parallel enrich job
                # can begin reading committed rows quickly.
                commit_every=80,
                # This archive's root id, not a path lookup:
                # the rows must land where the GUI reads.
                root_id=job.root_id,
            )
            totals.seen += stats.seen
            totals.new += stats.new
            totals.updated += stats.updated
            totals.errors += stats.errors
            totals.bytes_hashed += stats.bytes_hashed
        # Reached only when every root was walked end to end: cancellation and
        # errors both leave the run open, so neither can pass for full coverage.
        db.scan_run_finish(conn, run_id, totals, on_disk)
        job.message = f"{totals.seen} files scanned" + (
            f" · {totals.errors} unreadable" if totals.errors else ""
        )

    def _run_enrich(self, conn, job: Job, cancel):
        from ..metadata import enrich as enrich_mod

        prog = _JobProgress(job, cancel)
        root_ids = (job.root_id,) if job.root_id else None
        stats = enrich_mod.enrich(conn, self.cfg, progress=prog, root_ids=root_ids)
        job.message = (
            f"{stats.processed} processed, "
            f"{stats.with_takeout} Takeout-matched, "
            f"{stats.with_gps} with GPS"
        )

    def _run_dedup(self, conn, job: Job, cancel):
        from ..dedup import exact

        prog = _JobProgress(job, cancel)
        stats = exact.run(conn, self.cfg, progress=prog, root_id=job.root_id)
        # Hidden files are duplicate copies. They must never consume semantic
        # storage or appear as a stale vector if a prior run overlapped dedup.
        conn.execute(
            "DELETE FROM semantic_embeddings WHERE file_id IN (SELECT id FROM files WHERE hidden=1)"
        )
        # Record what this successful rebuild covered so dedup_needed() can
        # tell -- from the catalog alone, even after a restart -- that nothing
        # is owed, until a later scan/enrich invalidates it again
        # (_mark_dedup_owed). Sharing this commit with the DELETE above (not
        # exact.run()'s own, earlier commit) is fine: if the process dies
        # between them, the grouping already landed correctly and the only
        # cost is one redundant, harmless re-run that re-derives the same
        # grouping and then marks it.
        covered_files, covered_max_id = db.dedup_coverage(conn, job.root_id)
        db.dedup_mark_done(conn, job.root_id, covered_files, covered_max_id)
        conn.commit()
        job.message = (
            f"{stats.groups} groups, {stats.duplicate_files} duplicates, "
            f"{stats.reclaimable_bytes / 1e9:.1f} GB reclaimable"
        )

    def _run_places(self, conn, job: Job, cancel):
        # Keep map places in sync WITHOUT ever destroying user edits. Places are
        # durable entities: a root is clustered from scratch only the first time
        # (bootstrap); afterwards new geotagged files are attached incrementally
        # (assign_unplaced), so named/pinned places and manual attachments persist.
        # Each archive is now its own database, so this only ever touches the
        # one this job belongs to.
        from ..geo.clusters import assign_unplaced, cluster_places

        job.total, job.done = 1, 0
        has_places = conn.execute(
            "SELECT 1 FROM place_clusters WHERE root_id=? LIMIT 1", (job.root_id,)
        ).fetchone()
        touched = assign_unplaced(conn, job.root_id).points if has_places else 0
        if not has_places:
            cluster_places(conn, job.root_id)
        job.done = 1
        job.message = f"{touched} new geotagged files placed"

    def _run_detect(self, conn, job: Job, cancel):
        # Part of the automatic pipeline (after dedup). ONE decode per image runs
        # both detectors — people via SCRFD, animals via YOLOX — and the animal
        # boxes cross-check the faces inline (a face inside an animal box is
        # dropped from People). Detect in chunks and re-cluster people + pets after
        # each so both views fill in progressively during a long run.
        from ..detect import extract as dx
        from ..faces import cluster as fc
        from ..pets import cluster as pc

        # Progress is cumulative over ALL canonical media, not just this run's
        # backlog: total = every canonical image (+ video, once video detection
        # is enabled), done starts at how many are already detected. So the
        # bar/% match the "Detected N / total" tile and survive resuming across
        # restarts (no misleading per-run total). cfg is passed so the
        # population matches pending_count's (both honour detect_video_frames).
        total = dx.image_count(conn, self.cfg, job.root_id)
        already = max(0, total - dx.pending_count(conn, self.cfg, job.root_id))
        job.total, job.done = total, already
        # Load both detector model sets once and reuse across every chunk.
        face_be, pet_be = dx.make_backends(self.cfg, log=lambda m: setattr(job, "current", m))
        processed = faces_found = animals = suppressed = human_pets = 0
        turned = 0
        while True:
            if cancel.is_set():
                raise KeyboardInterrupt
            prog = _JobProgress(job, cancel, base=already + processed, fixed_total=True)
            st = dx.extract(
                conn,
                self.cfg,
                progress=prog,
                limit=self._DETECT_CHUNK,
                face_be=face_be,
                pet_be=pet_be,
                cache_dir=self.cfg.archive_cache_dir(job.root_id),
            )
            if st.processed == 0:
                break
            processed += st.processed
            faces_found += st.faces_found
            animals += st.animals
            suppressed += st.nonhuman_suppressed
            human_pets += st.human_animals_dropped
            turned += st.rotated
            job.current = "grouping people & pets…"
            fc.cluster_faces(conn, self.cfg)
            pc.cluster_pets(conn, self.cfg, root_id=job.root_id)

        # The backlog is empty, so an embedder migration staged earlier now has
        # every re-extracted face it needs: give the names, pins and review
        # answers back to the faces they belong to, then cluster once more so
        # those restored pins actually take effect. Only here, never mid-run: a
        # partially re-extracted archive would strand identities on faces that
        # have not been detected yet.
        from ..faces import migrate_adaface

        restored = 0
        if migrate_adaface.pending(conn):
            job.current = "restoring names and corrections…"
            restored = migrate_adaface.reattach(conn, self.cfg).faces_reattached
            fc.cluster_faces(conn, self.cfg)

        people = conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
        groups = conn.execute("SELECT COUNT(*) FROM pets").fetchone()[0]
        job.message = (
            f"{faces_found} faces · {people} people · {animals} animals · "
            f"{groups} pet groups"
            + (f" · {suppressed} animal-face FPs dropped" if suppressed else "")
            + (f" · {human_pets} people misread as pets" if human_pets else "")
            + (f" · {turned} photos turned upright" if turned else "")
            + (f" · {restored} identities restored" if restored else "")
        )

    def _run_face_cluster(self, conn, job: Job, cancel):
        from ..faces.cluster import cluster_faces

        job.current = "reclustering people after review…"
        stats = cluster_faces(conn, self.cfg)
        job.done = job.total = 1
        job.message = f"{stats.people} people · {stats.clustered} faces clustered"

    def _run_pet_cluster(self, conn, job: Job, cancel):
        # Mirrors _run_face_cluster; started after unmerge_pets so an undone
        # merge's fresh 'different' pet_links row takes effect immediately
        # rather than waiting for the next full detect chunk.
        from ..pets.cluster import cluster_pets

        job.current = "reclustering pets after review…"
        stats = cluster_pets(conn, self.cfg, root_id=job.root_id)
        job.done = job.total = 1
        job.message = f"{stats.pets} pets · {stats.clustered} detections clustered"

    def _run_semantic(self, job: Job, cancel):
        # Drain in passes so semantic indexing is ONE continuous job: each pass
        # handles the current backlog, then loops to pick up anything that became
        # pending while it ran (files the concurrent scan/enrich just added).
        # Restarting a fresh job per snapshot is what used to flicker the card
        # done→running; looping here keeps it steadily "running" until drained.
        total_indexed = total_skipped = total_failed = 0
        force = job.force
        while True:
            if cancel.is_set():
                raise KeyboardInterrupt
            indexed, skipped, failed, remaining = self._semantic_pass(job, cancel, force)
            total_indexed += indexed
            total_skipped += skipped
            total_failed += failed
            if remaining == 0:
                break
            force = False  # only the first pass honours a forced full reindex
        job.message = (
            f"{total_indexed} indexed, {total_skipped} skipped, {total_failed} errors"
            if (total_indexed or total_skipped or total_failed)
            else "semantic index is already current"
        )

    def _semantic_pass(self, job: Job, cancel, force: bool):
        """One snapshot pass. Returns (indexed, skipped, failed, rows_in_pass)."""
        from pathlib import Path

        from . import semantic

        # Snapshot candidates under a read-only connection. The API calls below
        # happen without the writer lock so local metadata/faces work continues.
        read_conn = db.open_readonly(self.cfg.archive_db_path(job.root_id))
        try:
            rows = semantic.pending_rows(read_conn, job.root_id, force=force)
            total, already = semantic.work_counts(read_conn, job.root_id, force=force)
        finally:
            read_conn.close()
        job.total, job.done = total, already
        if not rows:
            return (0, 0, 0, 0)
        indexed = skipped = failed = 0
        # A straight loop, one file at a time. The old batch-then-isolate retry
        # ladder existed because a single malformed input could 400 an entire
        # Voyage request and take its innocent neighbours down with it. Local
        # inference has no such failure mode: a file either decodes or it does
        # not, and media_part has already decided that. Batching would not even
        # pay for itself — on this CPU a batch of four costs four single
        # forwards — while one-at-a-time keeps the progress card truthful.
        # Per-item error capture stays: a truncated JPEG can still raise inside
        # PIL, and that must cost one file, not the pass.
        cache_dir = self.cfg.archive_cache_dir(job.root_id)
        for offset, row in enumerate(rows):
            if cancel.is_set():
                raise KeyboardInterrupt
            job.current = row["rel_path"]
            try:
                part, kind, reason = semantic.media_part(
                    self.cfg,
                    Path(row["root_path"]) / row["rel_path"],
                    row["ext"],
                    row["media_type"],
                    cache_dir,
                    row["rotate_deg"],
                    row["duration_s"],
                )
                if reason:
                    self._save_semantic_outcome(job.root_id, row, None, kind, reason)
                    skipped += 1
                else:
                    values = semantic.embed_part(self.cfg, part, kind)
                    if values is None:
                        self._save_semantic_outcome(
                            job.root_id,
                            row,
                            None,
                            kind,
                            f"unsupported {row['media_type']}: no frame could be decoded",
                        )
                        skipped += 1
                    else:
                        self._save_semantic_outcome(job.root_id, row, values, kind, None)
                        indexed += 1
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                # One line, no exc_info: this loop covers every file in the
                # archive (~150k), so a traceback each would fill the rotated log
                # before anything useful survived. The reason is also persisted
                # per row by _save_semantic_outcome, which the GUI surfaces.
                logger.warning("semantic indexing failed for file_id=%s: %s", row["id"], exc)
                self._save_semantic_outcome(job.root_id, row, None, None, str(exc))
                failed += 1
            job.done = already + offset + 1
        return (indexed, skipped, failed, len(rows))

    def _save_semantic_outcome(self, root_id, row, values, kind, reason):
        """Persist one result without taking the manager-wide pipeline lock.

        SQLite WAL plus its busy timeout serializes this tiny transaction against
        a metadata/faces batch, while the semantic worker remains independent of
        that job's long-lived manager lock. ``write_with_retry`` covers the rarer
        case where that still isn't enough (a batch write holding the writer past
        busy_timeout, see detect's commit budget); if the lock still won't clear,
        this row is left pending instead of turning into the card's error text --
        a lock is not a stage failure, and the next semantic pass will retry it
        since no embedding was ever written for it.
        """
        from . import semantic

        conn = db.connect(self.cfg.archive_db_path(root_id))
        try:
            # Dedup may have completed while this item was being embedded.
            # Only keep an outcome if this exact source remains canonical.
            current = conn.execute(
                "SELECT hidden, sha256 FROM files WHERE id=?", (row["id"],)
            ).fetchone()
            if current is None or current["hidden"] or current["sha256"] != row["sha256"]:
                return

            def _write():
                semantic.save_outcome(conn, self.cfg, row, values, kind, reason)
                conn.commit()

            try:
                db.write_with_retry(_write)
            except sqlite3.OperationalError:
                conn.rollback()
        finally:
            conn.close()
