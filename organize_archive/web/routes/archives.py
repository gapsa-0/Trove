"""The archive registry: what the picker lists."""

from __future__ import annotations

from ...services import archives
from ._request import Json, Request, ok_or_error


def archive_list(req: Request) -> dict:
    return {"archives": archives.archives(req.cfg)}


def add(req: Request) -> Json:
    return ok_or_error(archives.add_archive(req.cfg, req.body.get("path", "")))


def open_archive(req: Request) -> dict | Json:
    root_id = req.body.get("root_id")
    if not isinstance(root_id, int):
        return Json({"error": "root_id is required"}, 400)
    if not any(a["id"] == root_id and a["exists"] for a in archives.archives(req.cfg)):
        return Json({"error": "archive not found or unavailable"}, 404)
    req.jobs.open_archive(root_id)
    return {"ok": True}


def close(req: Request) -> dict:
    root_id = req.body.get("root_id")
    req.jobs.close_archive(root_id if isinstance(root_id, int) else None)
    return {"ok": True}


def remove(req: Request) -> Json:
    root_id = req.body.get("root_id")
    if not isinstance(root_id, int):
        return Json({"error": "root_id is required"}, 400)
    if not req.jobs.stop_archive(root_id):
        return Json({"error": "archive is still stopping; try again shortly"}, 409)
    return ok_or_error(archives.remove_archive(req.cfg, root_id))
