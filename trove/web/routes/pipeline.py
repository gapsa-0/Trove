"""Pipeline status: the same resolved stage list the scheduler acts on."""

from __future__ import annotations

from ...pipeline import stages, status
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
    return status.snapshot(req.cfg, req.jobs, rid, arch["path"])


def changed(req: Request) -> dict | Json:
    """Files may have arrived in this archive's folder; check sooner than the
    poll would have.

    Sent by the app when its window comes back to the front, because the shape
    of adding files is leaving Trove, dropping them in somewhere else, and
    coming back. It carries no claim about what changed and is not believed on
    that point: the pipeline re-walks and decides for itself.

    The reason this exists alongside the filesystem watcher rather than being
    replaced by it is that it works everywhere the watcher does not -- network
    shares that deliver no events, an exhausted inotify budget, an installation
    without the optional dependency. Between them, the case that is left is
    files arriving while nobody is looking at the window, which is the poll's.
    """
    rid = req.require_root()
    req.jobs.note_files_changed(rid)
    return {"ok": True}


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
