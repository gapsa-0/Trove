"""Pipeline status: the same resolved stage list the scheduler acts on."""

from __future__ import annotations

from ...services import archives
from .. import pipeline
from ._request import Json, Request


def snapshot(req: Request) -> dict | Json:
    # Single source of truth for pipeline status: the same resolved
    # stage list the scheduler acts on, so cards never disagree with
    # what's actually running.
    rid = req.root_id
    arch = next((a for a in archives.archives(req.cfg) if a["id"] == rid), None)
    if arch is None:
        return Json({"error": "unknown archive"}, 404)
    return pipeline.snapshot(req.cfg, req.jobs, rid, arch["path"])
