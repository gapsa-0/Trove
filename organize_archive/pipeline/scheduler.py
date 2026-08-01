"""The polling loop, and the decision of what to start next.

Split from ``manager.py`` because scheduling and running are different jobs:
this module decides *when* a stage should start, the manager owns the threads
and the registry that start it, and ``runners/`` performs the work. None of the
three knows the others' business.

The scheduler runs on exactly one thread, created and started by the
``JobManager`` that owns it. It never performs work itself -- every decision
ends in a ``manager.start(...)`` call, which hands off to a worker thread. See
``manager.py``'s threading contract for what that thread may touch.

State ownership: this object owns the poll interval, the wake event and the
stop event. It does **not** own ``_error_at`` -- the manager writes that when a
job fails and clears it when one succeeds, and ``_error_ready`` only reads it.
A second copy would drift.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)


def _state_note(stage: dict) -> str:
    """``running(1234/4213)`` for a stage with a live job, else just its state.

    The counts are what make a repeated tick line useful: two ticks showing the
    same state but a rising count mean progress, and the same count twice means
    the stage is genuinely stuck -- which is the question the log exists to
    answer and the one the state alone cannot.
    """
    progress = stage.get("progress") or {}
    total = progress.get("total")
    if stage["state"] == "running" and total:
        return f"running({progress.get('done', 0)}/{total})"
    return str(stage["state"])


class Scheduler:
    """Decides what to start next, on its own thread.

    Public surface, because tests drive it by hand rather than waiting on the
    timer: ``tick()`` makes one decision, ``stop()`` parks the loop, and
    ``interval`` is the current poll delay.
    """

    # Idle poll interval backs off (up to AUTO_MAX) when a tick finds nothing
    # to do, so a quiet archive doesn't get walked every few seconds forever.
    AUTO_MIN = 10
    AUTO_MAX = 300
    # After a job of some kind errors, wait this long before auto-restarting the
    # same kind, so a persistent failure backs off instead of spinning.
    ERROR_COOLDOWN = 120.0

    def __init__(self, manager):
        self._manager = manager
        self.interval = self.AUTO_MIN
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        """Begin polling. Called from ``JobManager.__init__`` so a manager is
        usable with no extra step."""
        self._thread.start()

    def stop(self) -> None:
        """Ask the loop to exit and wake it so it notices immediately."""
        self._stopping.set()
        self._wake.set()

    def stopping(self) -> bool:
        """Whether shutdown has been requested. Read by the background disk
        walk, which must not start a fresh walk on the way out."""
        return self._stopping.is_set()

    def join(self, timeout: float) -> None:
        self._thread.join(timeout=timeout)

    def nudge(self):
        """Wake the scheduler now after an archive has been opened."""
        self.interval = self.AUTO_MIN
        self._wake.set()

    def _loop(self):
        while not self._stopping.is_set():
            self._wake.wait(self.interval)
            self._wake.clear()
            if self._stopping.is_set():
                break
            try:
                acted = self.tick()
            except Exception:
                # The scheduler thread must outlive any single bad tick, so this
                # stays broad -- but an unlogged one made the whole pipeline look
                # merely idle.
                logger.exception("scheduler tick failed")
                acted = False
            self.interval = self.AUTO_MIN if acted else min(self.interval * 1.5, self.AUTO_MAX)

    def tick(self) -> bool:
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
        if self._manager._paused:
            logger.debug("tick: skipped, pipeline is paused")
            return False
        from ..services import archives
        from . import archives as archives_state
        from . import stages

        open_root_id = self._manager._open_root_id
        if open_root_id is None:
            logger.debug("tick: skipped, no archive is open")
            return False
        archive = next(
            (
                a
                for a in archives.archives(self._manager.cfg)
                if a["id"] == open_root_id and a["exists"]
            ),
            None,
        )
        if not archive:
            logger.debug("tick: skipped, archive root=%s is missing or unregistered", open_root_id)
            return False

        # The manager, not self: stage_states reads disk_count() and
        # dedup_needed() off it.
        states = stages.stage_states(
            self._manager.cfg, self._manager, open_root_id, archive["path"], allow_walk=True
        )
        stalled = self._stalled_kinds(states)
        lock_running = any(
            s["kind"] in stages.LOCK_KINDS and s["state"] == "running" for s in states
        )
        acted = False
        started_lock = False
        for s in states:
            kind, state = s["kind"], s["state"]
            if state not in ("queued", "error"):
                continue
            # A stage the user paused on its own is simply never started; its
            # siblings are untouched, which is the whole point of #32.
            if self._manager.stage_paused(stages.card_of(kind)):
                continue
            if state == "error" and not self._error_ready(open_root_id, kind):
                continue
            if kind in stages.PARALLEL_KINDS:
                if self._manager.active_kind(kind):
                    continue
            else:  # single-writer stage: at most one at a time
                if lock_running or started_lock:
                    continue
            # Scanning or enriching may change the file set, so a fresh duplicate
            # rebuild is owed once they finish.
            if kind in (stages.SCAN, stages.ENRICH):
                archives_state.mark_dedup_owed(self._manager.cfg, open_root_id)
            # dedup/places operate per-root via root_id and ignore root_path.
            path = None if kind in (stages.DEDUP, stages.PLACES) else archive["path"]
            if "error" not in self._manager.start(kind, open_root_id, path):
                acted = True
                if kind in stages.LOCK_KINDS:
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
                ",".join(f"{s['kind']}={_state_note(s)}" for s in states),
                ",".join(sorted(k for k, v in stalled.items() if v)) or "-",
            )
        return acted or outstanding

    def _stalled_kinds(self, states: list[dict]) -> dict[str, bool]:
        """Which stages cannot progress because of a per-stage pause: the paused
        ones, plus everything waiting on a stage that is itself stalled.

        ``states`` is in dependency order (stages.STAGES), so one forward pass
        propagates the whole chain -- pausing Deduplication also stops the map,
        detection and semantic stages that queue behind it.
        """
        from . import stages

        stalled: dict[str, bool] = {}
        for s in states:
            blocker = s["blocker"]
            stalled[s["kind"]] = self._manager.stage_paused(stages.card_of(s["kind"])) or bool(
                blocker and stalled.get(blocker)
            )
        return stalled

    def _error_ready(self, root_id: int, kind: str) -> bool:
        """True once a kind's post-error cooldown has elapsed (or none is set)."""
        at = self._manager._error_at.get((root_id, kind))
        return at is None or (time.monotonic() - at) >= self.ERROR_COOLDOWN
