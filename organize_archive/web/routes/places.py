"""The map: place clusters, raw geotagged points, and cluster membership."""

from __future__ import annotations

from typing import Any, cast

from ...db import database as db
from ...services import places
from ._request import NOT_FOUND, Json, Request, ok_or_error


def clusters(req: Request) -> dict:
    """Place clusters (grouped geotagged files) with at least the configured minimum media."""
    # place_clusters takes a plain int, not the usual int | None: raising here
    # (same ValueError, same 400) rather than a line later at req.db() is
    # observably identical, since this route does nothing with a missing root
    # other than fail.
    rid = req.require_root()
    return places.place_clusters(req.db(rid), rid, req.cfg.place_min_media)


def points(req: Request) -> dict:
    """Every geotagged file as a single un-clustered map point."""
    # The un-clustered map view: one point per geotagged file.
    rid = req.require_root()  # see clusters() above
    return places.place_points(req.db(rid), rid, req.cfg.place_min_media)


def merge_preview(req: Request) -> Json:
    """How spread out a prospective cluster merge would be, so the GUI can warn
    before it's confirmed."""
    # GET, not POST: this mutates nothing, it only answers "how
    # spread out would this merge be" so the GUI can decide
    # whether to warn before the user confirms the drag-merge.
    rid = req.root_id
    res = places.place_merge_preview(
        req.db(rid), req.one("a", int), req.one("b", int), req.cfg.place_merge_warn_km
    )
    return ok_or_error(res)


def cluster_members(req: Request) -> dict[str, Any] | Json:
    """One place cluster's member files, paginated."""
    rid = req.root_id
    c = places.place_cluster_members(
        req.db(rid),
        int(req.path.rsplit("/", 1)[1]),
        limit=req.limit(120, 500),
        offset=req.offset(),
    )
    return c if c else NOT_FOUND


def rename_cluster(req: Request) -> Json:
    """Rename a place cluster."""
    res = db.write_with_retry(
        lambda: places.rename_place_cluster(
            req.db(req.open_root_id),
            req.body.get("cluster_id"),
            (req.body.get("name") or "").strip(),
        )
    )
    return ok_or_error(res)


def merge_clusters(req: Request) -> Json:
    """Merge two place clusters the user confirmed are the same location, immediately."""
    res = db.write_with_retry(
        lambda: places.merge_place_clusters(
            req.db(req.open_root_id), req.body.get("a"), req.body.get("b"), req.body.get("name")
        )
    )
    return ok_or_error(res)


def unmerge_clusters(req: Request) -> Json:
    """Undo a place-cluster merge, restoring the dropped cluster verbatim."""
    # Unlike /api/faces/unmerge and /api/pets/unmerge, no job is
    # started here: places are durable (see place_merges' schema
    # comment), so unmerge_place_clusters is already a complete
    # restore, not a "delete a constraint and recluster" that
    # needs a background pass to finish the job.
    res = db.write_with_retry(
        lambda: places.unmerge_place_clusters(req.db(req.open_root_id), req.body.get("merge_id"))
    )
    return ok_or_error(res)


def set_item_place(req: Request) -> Json:
    """Attach or clear one file's place, depending on whether `clear` is set."""
    db_path = req.db(req.open_root_id)
    if req.body.get("clear"):
        res = db.write_with_retry(lambda: places.clear_place(db_path, req.body.get("file_id")))
    else:
        res = db.write_with_retry(
            lambda: places.set_place(db_path, req.body.get("file_id"), req.body.get("place_id"))
        )
    return ok_or_error(res)


def create_place(req: Request) -> Json:
    """Create a user-pinned place at a dropped coordinate, optionally attaching a file to it."""
    res = db.write_with_retry(
        lambda: places.create_place(
            req.db(req.body.get("root")),
            req.body.get("root"),
            # The JSON body is untyped at the boundary; create_place has
            # always received whatever the client sent under "name" with no
            # static check, same as before this pass -- just now spelled out.
            cast(str, req.body.get("name")),
            req.body.get("lat"),
            req.body.get("lon"),
            req.body.get("file_id"),
        )
    )
    return ok_or_error(res)
