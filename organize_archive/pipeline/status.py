"""The `/api/pipeline` payload: what the GUI polls about once a second.

``stages.py`` answers "what state is every stage in, and how do those roll up
into the five display cards" -- a question the scheduler asks too, which is why
the two can never disagree. This module answers the one question only the GUI
asks: what should the user be told *right now*. That is the resolved cards,
plus the running jobs that belong to no card, the pause overlay, and one
overall verdict for the sidebar chip.

Nothing here decides anything: every state it reports was resolved in
``stages``. It only chooses words.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..config import Config
from . import stages
from .stages import cards, is_stage_kind, job_progress

if TYPE_CHECKING:
    from .manager import JobManager


# Jobs the scheduler never queues on its own: a user action kicks them (a
# non-human review correction, undoing a merge). They aren't STAGES -- they have
# no backlog to count and no place in the dependency order -- but they DO take
# the single writer lock, so a user watching an unexplained pause deserves to
# see them. Reported separately from the cards so the Overview's stage grid
# keeps its fixed five-card shape.
_EXTRA_JOB_LABEL = {
    "face_cluster": "Updating people",
    "pet_cluster": "Updating pets",
}


def _extra_jobs(jobs: JobManager, root_id: int) -> list[dict[str, Any]]:
    """Running jobs that belong to no stage card, in the cards' output shape."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for j in jobs.list(root_id):
        kind = j["kind"]
        if j["status"] != "running" or is_stage_kind(kind) or kind in seen:
            continue
        seen.add(kind)
        out.append(
            {
                "id": kind,
                "label": _EXTRA_JOB_LABEL.get(kind, kind),
                "state": "running",
                "pending": None,
                "counted": False,
                "progress": job_progress(j),
                "next": False,
                "waiting_on": None,
                "message": f"{_EXTRA_JOB_LABEL.get(kind, kind)}…",
                "paused": False,
                "stalled": False,
            }
        )
    return out


def snapshot(cfg: Config, jobs: JobManager, root_id: int, root_path: str) -> dict[str, Any]:
    """The `/api/pipeline` payload: resolved cards plus one overall verdict.

    ``paused`` reflects the whole-pipeline pause (JobManager.paused()). While
    paused, a queued/error card must not claim work is imminent, so its
    message is overridden to "Paused" (its `state` is left untouched -- the
    client and scheduler both key off that); and ``overall`` becomes "paused"
    once nothing is actually still running.

    Individually paused stages (``jobs.paused_stages()``) work the same way one
    card at a time: their message becomes "Paused", and a card stuck behind one
    says so too instead of promising work that can never start.

    ``extra`` carries running non-stage jobs (see _extra_jobs). They count
    toward ``overall`` -- reporting "idle" while a recluster holds the writer
    would be a lie -- but stay out of ``stages`` so the Overview grid is
    unaffected; only the ambient sidebar chip reads them.
    """
    states = stages.stage_states(cfg, jobs, root_id, root_path)
    paused = bool(jobs.paused()) if hasattr(jobs, "paused") else False
    per_stage = frozenset(jobs.paused_stages()) if hasattr(jobs, "paused_stages") else frozenset()
    card_list = cards(states, per_stage)
    extra = _extra_jobs(jobs, root_id)
    for c in card_list:
        if c["state"] not in ("queued", "blocked", "error"):
            continue
        if paused or c["paused"]:
            c["next"] = False
            c["message"] = "Paused"
        elif c["stalled"]:
            c["next"] = False
            c["message"] = f"Waiting for {c['waiting_on'] or 'earlier steps'} (paused)"
    if extra or any(c["state"] == "running" for c in card_list):
        overall = "running"
    elif paused:
        overall = "paused"
    elif any(c["state"] in ("queued", "blocked", "error") and not c["stalled"] for c in card_list):
        overall = "working"
    elif any(c["state"] in ("queued", "blocked", "error") for c in card_list):
        # Everything still outstanding is stopped by a per-stage pause; the
        # pipeline is not idle, it is waiting on the user.
        overall = "paused"
    else:
        overall = "idle"
    return {
        "root_id": root_id,
        "overall": overall,
        "stages": card_list,
        "extra": extra,
        "paused": paused,
        "paused_stages": sorted(per_stage),
    }
