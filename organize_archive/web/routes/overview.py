"""Archive-wide summary routes: totals, the timeline histogram, and where the
dates came from."""

from __future__ import annotations

from ...services import overview
from ._request import Request


def summary(req: Request) -> dict:
    """Counts, size, media types and date range for one archive."""
    rid = req.root_id
    return overview.summary(req.db(rid), rid)


def timeline(req: Request) -> dict:
    rid = req.root_id
    return overview.timeline(
        req.db(rid),
        root_id=rid,
        bucket=req.one("bucket", str, "month"),
        year=req.one("year"),
        month=req.one("month"),
        person_ids=req.many("person"),
        cluster_id=req.one("place", int),
    )


def date_sources(req: Request) -> dict:
    rid = req.root_id
    return overview.date_sources(req.db(rid), rid)
