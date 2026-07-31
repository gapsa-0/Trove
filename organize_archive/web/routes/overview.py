"""Archive-wide summary routes: totals, the timeline histogram, and where the
dates came from."""

from __future__ import annotations

from ...services import overview
from ._request import Request


def summary(req: Request) -> dict:
    """Counts, size, media types and date range for one archive."""
    rid = req.root_id
    return overview.summary(req.db(rid), rid)
