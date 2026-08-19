"""Duplicate groups: the summary, the paginated listing, and which copies to keep."""

from __future__ import annotations

from ...db import database as db
from ...services import dups, dups_edit
from ._request import Json, Request, ok_or_error


def summary(req: Request) -> dict:
    """Unique-file, duplicate-group, pending and reclaimable-byte counts, plus the identical/visual split of the copies."""
    rid = req.root_id
    return dups.dup_summary(req.db(rid), rid)


def groups(req: Request) -> dict:
    """Duplicate groups with each member's role, optionally narrowed to those
    holding an identical copy or a visual match, and ordered by member count or
    (the default) by largest reclaimable bytes."""
    rid = req.root_id
    return dups.dup_groups(
        req.db(rid),
        rid,
        limit=req.limit(60, 200),
        offset=req.offset(),
        match=req.one("match"),
        sort=req.one("sort"),
    )


def keep(req: Request) -> Json:
    """Choose which copies of a duplicate group Browse shows. At least one."""
    res = db.write_with_retry(
        lambda: dups_edit.set_kept_copies(
            req.db(req.open_root_id),
            req.body.get("group_id"),
            req.body.get("file_ids"),
        )
    )
    return ok_or_error(res)
