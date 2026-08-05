"""The `/api/pipeline` payload: what the GUI polls about once a second.

``stages.py`` answers "what state is every stage in" and ``cards.py`` rolls
those up into the display cards -- questions the scheduler asks too, which is
why the two can never disagree. This module answers the one question only the
GUI asks: what should the user be told *right now*. That is the resolved cards,
plus three things the scheduler has no use for -- the running jobs that belong
to no card, the pause overlay (including the seconds where a pause has been
asked for but not yet reached), and one overall verdict for the sidebar chip.

Nothing here decides anything: every state it reports was resolved in
``stages``. It only chooses words.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..config import Config
from . import stages
from .cards import cards
from .stages import is_stage_kind, job_progress

if TYPE_CHECKING:
    from .manager import JobManager


# Jobs the scheduler never queues from the stage list. Two are kicked by a user
# action (a non-human review correction, undoing a merge); the third is the
# model download an archive owes when it is created. None of them is a STAGE --
# no backlog to count, no place in the dependency order -- but each is a reason
# the app is busy, so a user watching an unexplained pause deserves to see them.
# Reported separately from the cards so the Overview's stage grid keeps its
# fixed five-card shape.
_EXTRA_JOB_LABEL = {
    "face_cluster": "Updating people",
    "pet_cluster": "Updating pets",
    "models": "Downloading models",
}


def _extra_label(job: dict[str, Any]) -> str:
    """What to call one non-stage job while it runs.

    The model fetch answers with the download's own line ("downloading search
    model — 45% of 355 MB") whenever it has one. It reports no total, so the
    sidebar draws it the indeterminate bar it draws for any work with no
    percentage, and a fixed "Downloading models" over that bar is exactly the
    thing someone reads as hung after the first quiet minute of 689 MB.
    """
    kind = str(job["kind"])
    fixed = _EXTRA_JOB_LABEL.get(kind, kind)
    line = str(job.get("current") or "").strip()
    if kind != "models" or not line:
        return fixed
    return line[0].upper() + line[1:]


def _extra_jobs(jobs: JobManager, root_id: int) -> list[dict[str, Any]]:
    """Running jobs that belong to no stage card, in the cards' output shape."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for j in jobs.list(root_id):
        kind = j["kind"]
        if j["status"] != "running" or is_stage_kind(kind) or kind in seen:
            continue
        seen.add(kind)
        label = _extra_label(j)
        out.append(
            {
                "id": kind,
                "label": label,
                "state": "running",
                "pending": None,
                "counted": False,
                "progress": job_progress(j),
                "next": False,
                "pausing": False,
                "waiting_on": None,
                "message": f"{label}…",
                "paused": False,
                "stalled": False,
            }
        )
    return out


def _apply_pause(card_list: list[dict], extra: list[dict], paused: bool) -> None:
    """Say what a pause is actually doing, which is rarely "stopped, now".

    Pausing only *asks* the running job to stop, at its next batch checkpoint.
    A card that goes on saying "Scanning files…" for those seconds reads first
    as a button that did nothing, then as a stage that quit for no reason -- so
    it says "Pausing…", and keeps its bar moving because the work really is.
    Once stopped it keeps the bar it reached (``stages._stopped_progress``): a
    paused run is suspended, not discarded, and "Paused" on its own loses how
    far it got.
    """
    for c in card_list:
        stopping = paused or c["paused"]
        frozen = c.pop("stopped_progress", None)
        if c["state"] == "running":
            if stopping:
                c["pausing"] = True
                c["message"] = "Pausing…"
        elif c["state"] not in ("queued", "blocked", "error"):
            continue
        elif stopping:
            c["next"] = False
            c["message"] = "Paused"
            if frozen and not c["progress"]:
                c["progress"] = frozen
        elif c["stalled"]:
            c["next"] = False
            c["message"] = f"Waiting for {c['waiting_on'] or 'earlier steps'} (paused)"
    # The whole-pipeline pause cancels the non-stage jobs too
    # (JobManager._cancel_running takes no kinds); a per-stage one never does.
    for e in extra if paused else ():
        e["pausing"] = True
        e["message"] = "Pausing…"


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

    A pause is not instant, and ``_apply_pause`` is what says so honestly.

    ``extra`` carries running non-stage jobs (see _extra_jobs). They count
    toward ``overall`` -- reporting "idle" while a recluster holds the writer
    would be a lie -- but stay out of ``stages`` so the Overview grid is
    unaffected; only the ambient sidebar chip reads them.
    """
    states = stages.stage_states(cfg, jobs, root_id, root_path)
    paused = bool(jobs.paused()) if hasattr(jobs, "paused") else False
    per_stage = frozenset(jobs.paused_stages()) if hasattr(jobs, "paused_stages") else frozenset()
    enabled = cfg.archive_features(root_id)
    card_list = cards(states, per_stage, enabled)
    extra = _extra_jobs(jobs, root_id)
    _apply_pause(card_list, extra, paused)
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
