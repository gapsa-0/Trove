"""The registry: every kind of background work this app knows how to do.

``RUNNERS`` maps a job kind to the ``Runner`` that performs it. The manager
looks the kind up here and never learns what any of them do; a runner, in turn,
never learns which thread it is on. That split is the whole point of this
package -- adding a stage means writing one module here, registering it below,
and adding it to ``stages.STAGES``. Nothing in ``manager.py`` changes.

**A runner does not decide what is outstanding.** There is no ``pending()``
here on purpose: ``stages._pending()`` already computes the backlog for every
stage in one place, and both the scheduler and ``/api/pipeline`` read it
through ``stages.stage_states()``. A second count living next to each runner is
how the sidebar and the scheduler start disagreeing, and that class of bug is
miserable to reproduce. ``models`` -- the one runner here that is not a stage --
keeps to the same rule: what is left to download is ``services/models.missing``,
which the scheduler reads to decide whether to start it at all.
"""

from __future__ import annotations

from ..job import Runner
from . import (
    dedup,
    detect,
    enrich,
    face_cluster,
    meaning,
    models,
    pet_cluster,
    places,
    scan,
    semantic,
    text,
)

RUNNERS: dict[str, Runner] = {
    scan.RUNNER.kind: scan.RUNNER,
    enrich.RUNNER.kind: enrich.RUNNER,
    dedup.RUNNER.kind: dedup.RUNNER,
    places.RUNNER.kind: places.RUNNER,
    face_cluster.RUNNER.kind: face_cluster.RUNNER,
    pet_cluster.RUNNER.kind: pet_cluster.RUNNER,
    detect.RUNNER.kind: detect.RUNNER,
    semantic.RUNNER.kind: semantic.RUNNER,
    text.RUNNER.kind: text.RUNNER,
    meaning.RUNNER.kind: meaning.RUNNER,
    models.RUNNER.kind: models.RUNNER,
}

__all__ = ["RUNNERS", "Runner"]
