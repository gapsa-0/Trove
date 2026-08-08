"""Whether work is allowed to run, for the archive that is open.

Pause is a property of the *archive*, not of the app. Two switches make it up
and they are not the same question: the whole-pipeline pause stops everything,
and the per-stage set stops one card while the others keep going -- so the
second is only consulted when the first is off.

Both are held in memory and written through to the archive's own config. The
in-memory copy is authoritative on purpose: it is what gates the scheduler and
cancels running jobs, and a disk hiccup must not leave someone who just asked
to stop the CPU load still running. Persisting is best-effort and logged,
because the consequence of losing it is quiet -- the pause is honoured now and
forgotten on restart.

Lifted out of ``manager.py`` because it is state with rules of its own, and
because those rules kept having to be restated wherever the flags were touched.
The manager keeps the orchestration -- cancelling running jobs, nudging the
scheduler -- since that is the part that needs the job registry.
"""

from __future__ import annotations

import logging

from ..config import Config

logger = logging.getLogger(__name__)


class ArchivePause:
    """The open archive's two pause switches, loaded and written through.

    Read without a lock, like the attributes it replaced: each mutation is a
    single atomic rebind or a ``set`` mutation under the GIL, and no invariant
    spans the two. They describe whichever archive is currently open, and
    ``load`` replaces both when that changes -- which is what stops a pause
    following the user from one archive to another.
    """

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        # Until an archive is open the app-wide defaults stand, normally
        # "not paused".
        self.paused, stages_off = cfg.archive_pause(None)
        self.stages: set[str] = set(stages_off)
        self._root_id: int | None = None

    def load(self, root_id: int | None) -> None:
        """Adopt this archive's own pause state, replacing whatever was held."""
        self._root_id = root_id
        self.paused, stages_off = self._cfg.archive_pause(root_id)
        self.stages = set(stages_off)

    def stage_paused(self, card: str) -> bool:
        return card in self.stages

    def set_paused(self, value: bool) -> bool:
        """Set the whole-pipeline switch; returns what it is now."""
        self.paused = bool(value)
        logger.info("pipeline %s", "paused" if self.paused else "resumed")
        self._persist()
        return self.paused

    def set_stage(self, card: str, value: bool) -> None:
        """Pause or resume one stage card, leaving every other stage running."""
        if value:
            self.stages.add(card)
        else:
            self.stages.discard(card)
        logger.info("stage %s %s", card, "paused" if value else "resumed")
        self._persist()

    def _persist(self) -> None:
        """Record the open archive's pause state, so reopening it restores it.

        Nothing to record while no archive is open: the GUI can only reach
        these controls from an open one anyway.
        """
        if self._root_id is None:
            return
        try:
            self._cfg.set_archive_pause(self._root_id, self.paused, self.stages)
        except OSError:
            logger.warning("could not persist the pause state", exc_info=True)
