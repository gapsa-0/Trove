"""The map: place clusters, raw geotagged points, and cluster membership."""

from __future__ import annotations

from ...services import places
from ._request import NOT_FOUND, Request, ok_or_error


def clusters(req: Request) -> dict:
    rid = req.root_id
    return places.place_clusters(req.db(rid), rid, req.cfg.place_min_media)


def points(req: Request) -> dict:
    # The un-clustered map view: one point per geotagged file.
    rid = req.root_id
    return places.place_points(req.db(rid), rid, req.cfg.place_min_media)


def merge_preview(req: Request):
    # GET, not POST: this mutates nothing, it only answers "how
    # spread out would this merge be" so the GUI can decide
    # whether to warn before the user confirms the drag-merge.
    rid = req.root_id
    res = places.place_merge_preview(
        req.db(rid), req.one("a", int), req.one("b", int), req.cfg.place_merge_warn_km
    )
    return ok_or_error(res)


def cluster_members(req: Request):
    rid = req.root_id
    c = places.place_cluster_members(
        req.db(rid),
        int(req.path.rsplit("/", 1)[1]),
        limit=req.limit(120, 500),
        offset=req.offset(),
    )
    return c if c else NOT_FOUND
