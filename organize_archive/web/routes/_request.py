"""What a route handler is given, and what it may hand back.

A handler is a plain function ``(Request) -> result``. It never touches the
socket, never sees ``BaseHTTPRequestHandler`` and never writes a header, which
is what makes it callable from a test without a server. ``server.py`` owns the
translation in both directions: it builds the ``Request`` and it serialises
whatever comes back.

Handlers may return:

* any JSON-serialisable object -- sent as ``200 application/json``;
* ``Json(body, status)`` when the status is not 200;
* ``Raw(bytes, content_type)`` for a body built in memory (icons, manifest);
* ``FileBody(path)`` for a file streamed off disk, which is Range-aware.

Keeping that a small discriminated union, rather than letting handlers write to
the response themselves, is the whole reason the handlers are testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...config import Config
from ..jobs import JobManager


@dataclass(frozen=True)
class Json:
    """A JSON body with an explicit status."""

    body: Any
    status: int = 200


@dataclass(frozen=True)
class Raw:
    """Bytes with a content type the caller states, built in memory."""

    body: bytes
    content_type: str
    status: int = 200
    cache_control: str | None = None


@dataclass(frozen=True)
class FileBody:
    """A file streamed from disk. Honours ``Range``, so video seeking works."""

    path: Path
    content_type: str | None = None
    cache_control: str | None = None


# An unknown path and a legitimate "no such record" deliberately answer the
# same way, so a probe cannot tell which ids exist.
NOT_FOUND = Json({"error": "not found"}, 404)


def ok_or_error(res: dict) -> Json:
    """The service-call idiom: a result dict carrying ``"error"`` is a 400,
    anything else is a 200.

    Written once because twenty-odd mutation routes share it -- spelled out at
    each call site, one of them differing would be invisible to a reader.
    """
    return Json(res, 400 if "error" in res else 200)


@dataclass(frozen=True)
class Request:
    """One HTTP request, already parsed, plus the two process-wide handles a
    handler may need."""

    method: str
    path: str
    query: dict[str, list[str]]
    body: dict
    cfg: Config
    jobs: JobManager

    # -- query parameters --------------------------------------------------
    def one(self, name: str, cast=str, default=None):
        """One query value, cast. An empty value counts as absent, so
        ``?year=`` means "no year filter" rather than ``int("")``."""
        v = self.query.get(name, [None])[0]
        return cast(v) if v not in (None, "") else default

    def many(self, name: str, cast=int) -> list:
        """Read repeatable or comma-separated query values, preserving order."""
        out = []
        for value in self.query.get(name, []):
            for part in value.split(","):
                if part and (item := cast(part)) not in out:
                    out.append(item)
        return out

    def limit(self, default: int, cap: int) -> int:
        """``?limit=``, defaulted then clamped. The cap is per route and is the
        server's protection against a client asking for the whole archive."""
        return min(self.one("limit", int, default), cap)

    def offset(self) -> int:
        return self.one("offset", int, 0)

    # -- per-archive resolution ---------------------------------------------
    # Each archive is a fully separate database and cache dir; every request
    # that touches archive content must say which one. Content routes that
    # take no ``root`` query param (thumbnails, the original file) instead
    # resolve against whichever archive is currently open in the GUI, since
    # only one is ever browsed at a time. The same is true of mutations that
    # act on an id (person, cluster, face, pet...) rather than a file the
    # caller already knows the root of -- the frontend never sends one for
    # them either, so they resolve against the open archive too. The one
    # exception is /api/places/create, which is given an explicit ``root``
    # in its body.
    @property
    def root_id(self) -> int | None:
        """The ``?root=`` archive id, or ``None``. Turning that ``None`` into an
        error is ``db()``/``cache()``'s job, so every route fails the same way."""
        return self.one("root", int)

    @property
    def open_root_id(self) -> int | None:
        """The archive the GUI currently has open."""
        return self.jobs.current_root_id()

    def db(self, root_id) -> str:
        if root_id is None:
            raise ValueError("root is required")
        return self.cfg.archive_db_path(root_id)

    def cache(self, root_id) -> str:
        if root_id is None:
            raise ValueError("root is required")
        return self.cfg.archive_cache_dir(root_id)
