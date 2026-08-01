"""The API, as three tables instead of two long if/elif chains.

``GET_ROUTES`` / ``POST_ROUTES`` map an exact path to a handler;
``GET_PREFIX_ROUTES`` holds the genuinely path-parameterised ones
(``/thumb/<id>``, ``/api/faces/person/<id>``...). Between them they are the
single source of truth for what this server answers -- ``docs/dev/api.md`` is
generated from them, and ``tests/gui/test_api_routes.py`` checks them against
its own declared list.

Two dispatch rules, both load-bearing:

* **Exact wins over prefix.** ``/api/map/cluster/merge-preview`` is an exact
  route sitting under the ``/api/map/cluster/`` prefix; looking the dict up
  first is what stops the prefix swallowing it. The old chain relied on branch
  order for the same thing, where it was invisible.
* **Prefix order is preserved** from that chain even though no prefix currently
  shadows another, so adding one that does cannot quietly change the winner.

Handlers live one module per domain, mirroring ``services/``. A handler parses,
calls one service function and returns -- it holds no SQL, and if it grows past
about twenty lines the logic belongs in ``services/`` instead.
"""

from __future__ import annotations

from collections.abc import Callable

from . import (
    archives,
    browse,
    dups,
    media,
    overview,
    people,
    pets,
    pipeline,
    places,
    search,
    static,
)
from ._request import NOT_FOUND, FileBody, Json, Raw, Request, ok_or_error

__all__ = [
    "GET_PREFIX_ROUTES",
    "GET_ROUTES",
    "NOT_FOUND",
    "POST_ROUTES",
    "FileBody",
    "Json",
    "Raw",
    "Request",
    "ok_or_error",
    "prefix_handler",
]

Handler = Callable[[Request], object]

GET_ROUTES: dict[str, Handler] = {
    "/api/health": static.health,
    "/api/archives": archives.archive_list,
    "/api/settings": static.settings,
    "/api/pipeline": pipeline.snapshot,
    "/api/summary": overview.summary,
    "/api/timeline": overview.timeline,
    "/api/dates/sources": overview.date_sources,
    "/api/dups/summary": dups.summary,
    "/api/dups": dups.groups,
    "/api/media": browse.media,
    "/api/browse/filters": browse.filters,
    "/api/folders": browse.folders,
    "/api/browse/semantic/status": search.semantic_status,
    "/api/browse/semantic/search": search.semantic_search,
    "/api/map/clusters": places.clusters,
    "/api/map/points": places.points,
    "/api/map/cluster/merge-preview": places.merge_preview,
    "/api/faces/summary": people.summary,
    "/api/faces/persons": people.persons,
    "/api/faces/suggestions": people.suggestions,
    "/api/pets/summary": pets.summary,
    "/api/pets": pets.groups,
    "/api/pet/detections": pets.detections,
    "/api/nonhuman": pets.nonhuman,
    "/": static.index,
    "/index.html": static.index,
    "/manifest.webmanifest": static.manifest,
    "/sw.js": static.service_worker,
}

# Ordered: the first matching prefix wins. See the module docstring.
GET_PREFIX_ROUTES: tuple[tuple[str, Handler], ...] = (
    ("/api/item/", browse.item),
    ("/api/map/cluster/", places.cluster_members),
    ("/api/faces/person/", people.person),
    ("/api/pet/", pets.group),
    ("/icon-", static.icon),
    ("/vendor/", static.vendor),
    ("/archivethumb/", media.archive_thumb),
    ("/thumb/", media.thumb),
    ("/faceThumb/", media.face_thumb),
    ("/animalThumb/", media.animal_thumb),
    ("/file/", media.original),
)

POST_ROUTES: dict[str, Handler] = {
    "/api/archives": archives.add,
    "/api/archive/open": archives.open_archive,
    "/api/archive/close": archives.close,
    "/api/archive/remove": archives.remove,
    "/api/pipeline/pause": pipeline.pause,
}


def prefix_handler(table: tuple[tuple[str, Handler], ...], path: str) -> Handler | None:
    for prefix, handler in table:
        if path.startswith(prefix):
            return handler
    return None
