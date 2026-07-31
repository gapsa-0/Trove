"""The media grid and the single-item view."""

from __future__ import annotations

from ...services import browse
from ._request import NOT_FOUND, Json, Request


def item(req: Request) -> dict | Json:
    """One file's full detail page, by id.

    Takes no ``root``: the frontend never sends one for an item it reached from
    the open archive's grid, so this resolves against whichever archive is open,
    the same way thumbnails and originals do.
    """
    rid = req.open_root_id
    it = (
        browse.item(req.db(rid), int(req.path.rsplit("/", 1)[1]), req.cfg.place_min_media)
        if rid
        else None
    )
    return it if it else NOT_FOUND
