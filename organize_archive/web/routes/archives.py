"""The archive registry: what the picker lists."""

from __future__ import annotations

from ...services import archives
from ._request import Json, Request, ok_or_error


def archive_list(req: Request) -> dict:
    """Every registered archive, for the picker."""
    return {"archives": archives.archives(req.cfg)}


def feature_list(req: Request) -> dict:
    """Every feature an archive can be given, for the setup panel."""
    return {"features": archives.features(req.cfg)}


def _chosen(body: dict) -> list[str] | None:
    """The feature ids in a request body, or None when it names none.

    None and ``[]`` mean different things and both arrive as JSON: no key at all
    is "leave this alone" (or, on creation, "give it everything"), while an
    empty list is a deliberate choice of only the required features.
    """
    value = body.get("features")
    return [str(v) for v in value] if isinstance(value, list) else None


def add(req: Request) -> Json:
    """Register a new archive by folder path, preparing its private database."""
    name = req.body.get("name")
    return ok_or_error(
        archives.add_archive(
            req.cfg,
            req.body.get("path", ""),
            str(name) if isinstance(name, str) else None,
            _chosen(req.body),
        )
    )


def configure(req: Request) -> Json:
    """Rename an archive, or change which features it runs."""
    root_id = req.body.get("root_id")
    if not isinstance(root_id, int):
        return Json({"error": "root_id is required"}, 400)
    name = req.body.get("name")
    return ok_or_error(
        archives.configure_archive(
            req.cfg,
            root_id,
            str(name) if isinstance(name, str) else None,
            _chosen(req.body),
        )
    )


def open_archive(req: Request) -> dict | Json:
    """Open one registered archive so the GUI starts serving its content."""
    root_id = req.body.get("root_id")
    if not isinstance(root_id, int):
        return Json({"error": "root_id is required"}, 400)
    if not any(a["id"] == root_id and a["exists"] for a in archives.archives(req.cfg)):
        return Json({"error": "archive not found or unavailable"}, 404)
    req.jobs.open_archive(root_id)
    return {"ok": True}


def close(req: Request) -> dict:
    """Close the currently open archive, stopping its background jobs."""
    root_id = req.body.get("root_id")
    req.jobs.close_archive(root_id if isinstance(root_id, int) else None)
    return {"ok": True}


def remove(req: Request) -> Json:
    """Forget an archive: delete its private database and cache wholesale."""
    root_id = req.body.get("root_id")
    if not isinstance(root_id, int):
        return Json({"error": "root_id is required"}, 400)
    if not req.jobs.stop_archive(root_id):
        return Json({"error": "archive is still stopping; try again shortly"}, 409)
    return ok_or_error(archives.remove_archive(req.cfg, root_id))
