"""Pipeline status: the same resolved stage list the scheduler acts on."""

from __future__ import annotations

from typing import cast

from ...pipeline import stages
from ...services import archives
from ._request import Json, Request


def snapshot(req: Request) -> dict | Json:
    """The resolved status of every pipeline stage for one archive."""
    # Single source of truth for pipeline status: the same resolved
    # stage list the scheduler acts on, so cards never disagree with
    # what's actually running.
    rid = req.root_id
    arch = next((a for a in archives.archives(req.cfg) if a["id"] == rid), None)
    if arch is None:
        return Json({"error": "unknown archive"}, 404)
    # arch is only found when its "id" == rid, and archive ids are always
    # int, never None, so a match proves rid is an int here -- a narrowing
    # mypy can't follow through the generator above. Not require_root():
    # that would turn a missing ?root= into a 400 "root is required"
    # instead of this route's existing 404 "unknown archive".
    return stages.snapshot(req.cfg, req.jobs, cast(int, rid), arch["path"])


def pause(req: Request) -> dict | Json:
    """Pause or resume the whole pipeline, or a single stage's card if `stage` is given."""
    # Without "stage" this is the whole-pipeline switch; with one it
    # pauses that single card (scan/dedup/detect/places/semantic) and
    # leaves the rest of the pipeline running.
    paused = req.body.get("paused")
    stage = req.body.get("stage")
    if not isinstance(paused, bool):
        return Json({"error": "paused (bool) is required"}, 400)
    if stage is not None and stage not in stages.CARD_ORDER:
        return Json({"error": f"unknown stage: {stage}"}, 400)
    if stage is None:
        req.jobs.set_paused(paused)
        return {"paused": req.jobs.paused()}
    req.jobs.set_stage_paused(stage, paused)
    return {
        "paused": req.jobs.paused(),
        "paused_stages": sorted(req.jobs.paused_stages()),
    }
