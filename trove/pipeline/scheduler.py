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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # TYPE_CHECKING-only, same reasoning as stages.py: manager.py imports this
    # module at the top level to construct its Scheduler, so a real import
    # here would close the loop. `from __future__ import annotations` means
    # this name is never looked up at runtime.
    from .manager import JobManager

logger = logging.getLogger(__name__)


def _state_note(stage: dict[str, Any]) -> str:
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
    # The model fetch backs off much further than a stage does. A stage that
    # fails has a card saying so and a user watching it; a failed fetch is
    # invisible by design (the stage will still fetch its own weights), and a
    # retry restarts a download from zero rather than resuming it -- so retrying
    # every two minutes on a flaky connection would spend bandwidth all day with
    # nothing to show for it.
    FETCH_COOLDOWN = 900.0

    def __init__(self, manager: JobManager):
        self._manager = manager
        # float, not int: the idle backoff below multiplies it by 1.5 each
        # quiet tick, capped at AUTO_MAX.
        self.interval: float = self.AUTO_MIN
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

    def nudge(self) -> None:
        """Wake the scheduler now after an archive has been opened."""
        self.interval = self.AUTO_MIN
        self._wake.set()

    def _loop(self) -> None:
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

    def _open_archive(self) -> tuple[dict[str, Any], int] | None:
        """The open archive's registry entry, or None with a reason logged.

        Every "nothing to do" exit from tick() lands here, so the debug log has
        one place that answers why a tick did nothing at all.
        """
        from ..services import archives

        if self._manager.paused():
            logger.debug("tick: skipped, pipeline is paused")
            return None
        open_root_id = self._manager._open_root_id
        if open_root_id is None:
            logger.debug("tick: skipped, no archive is open")
            return None
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
            return None
        return archive, open_root_id

    def _may_start(
        self, s: dict, open_root_id: int, lock_running: bool, started_lock: bool
    ) -> bool:
        """Whether this stage may start on this tick."""
        from . import stages

        kind, state = s["kind"], s["state"]
        if state not in ("queued", "error"):
            return False
        # A stage the user paused on its own is simply never started; its
        # siblings are untouched, which is the whole point of #32.
        if self._manager.stage_paused(stages.card_of(kind)):
            return False
        if state == "error" and not self._error_ready(open_root_id, kind):
            return False
        if kind in stages.PARALLEL_KINDS:
            return not self._manager.active_kind(kind)
        # single-writer stage: at most one at a time
        return not (lock_running or started_lock)

    def _start_ready(self, states: list[dict], open_root_id: int, archive: dict[str, Any]) -> bool:
        """Start every stage that may start now. True if anything did."""
        from . import archives as archives_state
        from . import stages

        lock_running = any(
            s["kind"] in stages.LOCK_KINDS and s["state"] == "running" for s in states
        )
        awaiting = self._awaiting_weights(open_root_id)
        acted = False
        started_lock = False
        for s in states:
            kind = s["kind"]
            # Its weights are being fetched right now. Starting would fetch them
            # a second time, in parallel: the stage runs its own ensure_models
            # and neither call knows about the other.
            if kind in awaiting:
                continue
            if not self._may_start(s, open_root_id, lock_running, started_lock):
                continue
            # Scanning or enriching may change the file set, so a fresh duplicate
            # rebuild is owed once they finish.
            if kind in (stages.SCAN, stages.ENRICH):
                archives_state.mark_dedup_owed(self._manager.cfg, open_root_id)
            # dedup/places operate per-root via root_id and ignore root_path.
            path = None if kind in (stages.DEDUP, stages.PLACES) else archive["path"]
            # Deciding this stage was owed meant counting the files on disk, so
            # hand the scan the answer rather than letting it walk the tree a
            # second time to reach the same number. Served from the cache that
            # count went into, so this costs nothing.
            on_disk = (
                self._manager.disk_count(open_root_id, archive["path"], allow_walk=False)
                if kind == stages.SCAN
                else None
            )
            if "error" not in self._manager.start(kind, open_root_id, path, files_on_disk=on_disk):
                acted = True
                if kind in stages.LOCK_KINDS:
                    started_lock = True
        return acted

    def _awaiting_weights(self, open_root_id: int) -> frozenset[str]:
        """Stage kinds the running fetch has not delivered the weights for yet.

        Per feature rather than "anything that downloads something": detect and
        semantic fetch disjoint files, so holding detection back for the whole of
        a 689 MB SigLIP download would be an invented dependency -- and a stalled
        download would then stall a stage it has nothing to do with. As each
        feature's weights land, the stages that were waiting on those weights
        stop waiting, without anything having to be told.

        Empty whenever no fetch is running, which is the overwhelmingly common
        case and the reason this asks the filesystem nothing then.
        """
        from .. import features
        from ..services import models as model_service
        from .runners import models

        if not self._manager.active_kind(models.KIND):
            return frozenset()
        cfg = self._manager.cfg
        owed = model_service.missing(cfg, cfg.archive_features(open_root_id))
        return features.stage_kinds(owed)

    def _start_fetch(self, open_root_id: int) -> bool:
        """Start the model download this archive still owes, if it owes one.

        Runs before the stages are considered, and that order is the point: an
        archive whose scan finished long ago has its detect stage queued this
        very tick, and letting it start first would put the download inside the
        stage again -- which is the whole thing this job exists to move.

        Cheap to ask (a handful of stats, no network) and false the moment the
        weights are here, which for most archives on most ticks is immediately.
        """
        from ..services import models as model_service
        from .runners import models

        if self._manager.active_kind(models.KIND):
            return False
        if not self._error_ready(open_root_id, models.KIND, self.FETCH_COOLDOWN):
            return False
        enabled = self._manager.cfg.archive_features(open_root_id)
        if not model_service.missing(self._manager.cfg, enabled):
            return False
        return "error" not in self._manager.start(models.KIND, open_root_id)

    def tick(self) -> bool:
        """One scheduling decision, driven entirely by the same pipeline snapshot
        the GUI renders. Starts every stage that is ready to run and returns True
        while any work is outstanding, so the loop keeps the fast interval until
        the pipeline is fully idle.

        Readiness (deps satisfied, backend available, not already running) is
        already resolved in the snapshot: a stage is ``queued`` exactly when it
        should start now. Parallel kinds (scan ∥ enrich ∥ semantic) start
        together; the DB-writer stages (dedup → places → detect) start one at
        a time, matching the single write lock.

        The one job started here that is not a stage is the model fetch, which
        belongs to no card and reads its own outstanding work (see
        ``_start_fetch``).
        """
        from . import stages
        from .runners import models

        opened = self._open_archive()
        if opened is None:
            return False
        archive, open_root_id = opened

        fetching = self._start_fetch(open_root_id)
        # The manager, not self: stage_states reads disk_count() and
        # dedup_needed() off it.
        states = stages.stage_states(
            self._manager.cfg, self._manager, open_root_id, archive["path"], allow_walk=True
        )
        stalled = self._stalled_kinds(states)
        acted = self._start_ready(states, open_root_id, archive) or fetching

        # Keep polling promptly while anything is running or waiting to run.
        # Work held back by a per-stage pause does not count -- neither the
        # paused stage nor anything queued behind it can start until the user
        # says so, and counting it would pin the scheduler (and its disk walk)
        # to the fast interval indefinitely. A download in flight does count:
        # the stages waiting on it are the ones to start the moment it lands.
        outstanding = self._manager.active_kind(models.KIND) or any(
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

    def _error_ready(self, root_id: int, kind: str, cooldown: float | None = None) -> bool:
        """True once a kind's post-error cooldown has elapsed (or none is set)."""
        at = self._manager._error_at.get((root_id, kind))
        return at is None or (time.monotonic() - at) >= (cooldown or self.ERROR_COOLDOWN)
