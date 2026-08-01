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


def media(req: Request) -> dict:
    rid = req.root_id
    # Absent means "no filter"; only the two explicit values narrow
    # the grid, so a stray ?indexed=maybe cannot silently hide media
    # the user asked to see.
    indexed = {"yes": True, "no": False}.get(req.one("indexed"))
    located = {"yes": True, "no": False}.get(req.one("located"))
    return browse.media(
        req.db(rid),
        root_id=rid,
        year=req.one("year"),
        month=req.one("month"),
        mtype=req.one("type"),
        person_ids=req.many("person"),
        cluster_id=req.one("place", int),
        sort="oldest" if req.one("sort") == "oldest" else "newest",
        limit=req.limit(120, 500),
        offset=req.offset(),
        indexed=indexed,
        located=located,
    )


def filters(req: Request) -> dict:
    rid = req.root_id
    return browse.browse_filters(req.db(rid), rid)


def folders(req: Request) -> dict:
    rid = req.root_id
    return browse.folders(req.db(rid), rid, limit=req.limit(120, 500))
