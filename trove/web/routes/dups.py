"""Duplicate groups: the summary and the paginated group listing."""

from __future__ import annotations

from ...services import dups
from ._request import Request


def summary(req: Request) -> dict:
    """Unique-file, duplicate-group, pending and reclaimable-byte counts, broken down by match type and media type."""
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
