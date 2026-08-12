"""The media grid and the single-item view."""

from __future__ import annotations

from ...db import database as db
from ...services import browse, item_detail
from ...services.types import MediaPage
from ._request import NOT_FOUND, Json, Request, ok_or_error


def item(req: Request) -> dict | Json:
    """One file's full detail page, by id.

    Takes ``root`` when the caller knows it, and falls back to whichever archive
    the GUI has open when it does not (thumbnails and originals still resolve
    that second way alone).

    The explicit form exists because the two are not equally available. The grid
    is drawn from ``?root=``, so a tile can be on screen and clickable before the
    separate "open this archive" POST has landed -- and the viewer then asked
    for an item against an archive the server did not yet consider open, and got
    a 404 for a file that plainly exists. Browse never had the problem because
    it always named the archive it was reading; this now does the same.
    """
    rid = req.root_id or req.open_root_id
    it = (
        item_detail.item(req.db(rid), int(req.path.rsplit("/", 1)[1]), req.cfg.place_min_media)
        if rid
        else None
    )
    return it if it else NOT_FOUND


def media(req: Request) -> MediaPage:
    """The media grid: filtered, sorted and paginated files for the archive."""
    rid = req.root_id
    # Absent means "no filter"; only the two explicit values narrow
    # the grid, so a stray ?indexed=maybe cannot silently hide media
    # the user asked to see.
    # Keyed on `str | None` so an absent param looks up as cleanly as a
    # nonsense one: both miss, and both mean "no filter".
    tristate: dict[str | None, bool] = {"yes": True, "no": False}
    indexed = tristate.get(req.one("indexed"))
    located = tristate.get(req.one("located"))
    return browse.media(
        req.db(rid),
        root_id=rid,
        year=req.one("year"),
        month=req.one("month"),
        mtype=req.one("type"),
        # `name` is Browse's search box on an archive with no search feature:
        # every word of it has to appear in the file's own name.
        name=req.one("name"),
        person_ids=req.many("person"),
        pet_ids=req.many("pet"),
        cluster_id=req.one("place", int),
        sort="oldest" if req.one("sort") == "oldest" else "newest",
        limit=req.limit(120, 500),
        offset=req.offset(),
        indexed=indexed,
        located=located,
    )


def filters(req: Request) -> dict:
    """The distinct filter values (years, types, folders...) the Browse UI offers."""
    rid = req.root_id
    return browse.browse_filters(req.db(rid), rid)


def folders(req: Request) -> dict:
    """The folder tree with per-folder file counts."""
    rid = req.root_id
    return browse.folders(req.db(rid), rid, limit=req.limit(120, 500))


def set_date(req: Request) -> Json:
    """Set one file's date by hand, as a manual override."""
    res = db.write_with_retry(
        lambda: browse.set_date(
            req.db(req.open_root_id),
            req.body.get("file_id"),
            # Absent stays the service's own "bad date" error; a present
            # non-string is a 400 rather than an AttributeError inside strip().
            req.body_str("datetime", default=""),
        )
    )
    return ok_or_error(res)
