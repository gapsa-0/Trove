"""Routes for the reference pages: the index rail's catalogue, and one page.

Two routes rather than a path-parameterised one because there are exactly two
questions the reader's screen asks, and neither is about an archive: the pages
here describe what the app does, not what any one folder contains, which is why
they answer without a ``root``.
"""

from __future__ import annotations

from dataclasses import asdict

from .. import docs
from ._request import NOT_FOUND, Json, Request


def catalogue(req: Request) -> dict:
    """Every reference page in reading order, for the index rail."""
    return {"pages": [asdict(entry) for entry in docs.catalogue()]}


def page(req: Request) -> dict | Json:
    """One reference page, rendered, by ``?slug=``."""
    return docs.page(req.one("slug") or "") or NOT_FOUND
