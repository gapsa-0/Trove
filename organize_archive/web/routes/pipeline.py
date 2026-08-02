"""Pipeline status: the same resolved stage list the scheduler acts on."""

from __future__ import annotations

from ...pipeline import stages
from ...services import archives
from ._request import Json, Request


def snapshot(req: Request) -> dict | Json:
    """The resolved status of every pipeline stage for one archive."""
    # Single source of truth for pipeline status: the same resolved
    # stage list the scheduler acts on, so cards never disagree with
    # what's actually running.
    # A missing ?root= is a malformed request (400), the same as everywhere
    # else in the app; a ?root= naming an archive that does not exist is a 404.
    # This route used to answer 404 for both, because it read root_id directly
    # and let the "unknown archive" branch catch None on the way past. Nothing
    # in web/static/js distinguishes the two -- every caller sends a root -- so
    # the only thing that changed is that the codes now mean what they say.
    rid = req.require_root()
    arch = next((a for a in archives.archives(req.cfg) if a["id"] == rid), None)
    if arch is None:
        return Json({"error": "unknown archive"}, 404)
    return stages.snapshot(req.cfg, req.jobs, rid, arch["path"])


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
