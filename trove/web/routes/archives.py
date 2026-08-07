"""The archive registry: what the picker lists."""

from __future__ import annotations

from typing import Any

from ... import features as feature_catalog
from ...services import archives
from .. import docs
from ._request import Json, Request, ok_or_error


def _ways(enabled: list[str]) -> list[dict[str, Any]]:
    """What Browse's search box can be asked of this archive, ready to draw.

    Composed here rather than in the frontend because every string in it is the
    catalogue's (``features.search_ways``) and every documentation slug is the
    pages' own frontmatter (``docs.slug_for_feature``). Browse is the fourth
    screen to name this work, and the two tables that stop the four disagreeing
    are both on this side; a JS copy of either is how they start to.

    Attached to the archive the picker already hands the client rather than
    served on its own, so opening Browse costs no extra request and its headings
    are drawn on the first paint rather than filled in afterwards.
    """
    out = []
    for way in feature_catalog.search_ways(enabled):
        readers = []
        for fid in way.readers:
            feature = feature_catalog.by_id(fid)
            if feature is None:
                continue
            readers.append(
                {
                    "id": fid,
                    "label": feature.label,
                    "icon": feature.icon,
                    "docs": docs.slug_for_feature(fid),
                }
            )
        # The way no feature owns still gets somewhere to read about it: the
        # page about searching from Browse, which is where it is described.
        if not readers:
            readers = [{"id": "", "label": way.label, "icon": way.icon, "docs": "browse"}]
        out.append(
            {
                "id": way.id,
                "label": way.label,
                "icon": way.icon,
                "matches": way.matches,
                "always": way.always,
                "readers": readers,
            }
        )
    return out


def _with_ways(entry: dict[str, Any]) -> dict[str, Any]:
    """One archive entry, plus the search ways its feature set gives it."""
    return {**entry, "ways": _ways(list(entry.get("features") or []))}


def archive_list(req: Request) -> dict:
    """Every registered archive, for the picker."""
    return {"archives": [_with_ways(a) for a in archives.archives(req.cfg)]}


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


def _answered_with_ways(result: dict[str, Any]) -> Json:
    """A registry answer, carrying its ways when it carries a feature set.

    A newly created archive is opened straight from this response rather than
    from a reloaded picker list, so without this the first screen after setup
    would be a Browse with no headings.
    """
    if "features" in result and "error" not in result:
        result = _with_ways(result)
    return ok_or_error(result)


def check(req: Request) -> dict[str, Any]:
    """Whether a folder could become an archive, asked before setup opens.

    The picker asks as soon as a folder is chosen, so "that folder is already
    an archive" arrives instead of the setup screen rather than after someone
    has configured one. Same function the creation path runs, so the two
    answers cannot come apart.
    """
    return archives.check_folder(req.cfg, req.one("path") or "")


def add(req: Request) -> Json:
    """Register a new archive by folder path, preparing its private database."""
    name = req.body.get("name")
    return _answered_with_ways(
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
    return _answered_with_ways(
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
