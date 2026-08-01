"""The archive registry: what the picker lists."""

from __future__ import annotations

from ...services import archives
from ._request import Request


def archive_list(req: Request) -> dict:
    return {"archives": archives.archives(req.cfg)}
