"""Duplicate groups: the summary and the paginated group listing."""

from __future__ import annotations

from ...services import dups
from ._request import Request


def summary(req: Request) -> dict:
    rid = req.root_id
    return dups.dup_summary(req.db(rid), rid)


def groups(req: Request) -> dict:
    rid = req.root_id
    return dups.dup_groups(req.db(rid), rid, limit=req.limit(60, 200), offset=req.offset())
