"""The drift guard: what the server actually routes must match what is tested.

Split out of ``test_api_routes.py``. The lists below are the declared surface --
every path the GUI serves -- and the tests check them against the tables in
``web/routes/``. A route added or dropped without a matching update here fails
a test instead of silently drifting out of what the suite covers.

It also parses ``server.py`` for ``path ==`` / ``path in (...)`` /
``path.startswith(...)`` conditions even though there are none left: that half
guards against a hand-rolled branch reappearing beside the tables, which would
be a route the generated ``docs/dev/api.md`` never sees.
"""

from __future__ import annotations

import re
from pathlib import Path

from trove.web import server

# ---------------------------------------------------------------------------
# Drift guard: the routes server.py actually serves must match this file's
# declared route lists.
#
# Routes live in two places while stage 08's rewrite is under way: the tables in
# ``web/routes/`` and whatever is left of do_GET/do_POST's if/elif chains. The
# guard asserts against their *union*, so it stays exact from one end of the
# rewrite to the other -- with the tables empty it is the original chain-scraping
# check, and once the chains are gone it is a plain read of the tables.
# ---------------------------------------------------------------------------

# GET, exact (35: 31 /api + 4 non-api).
GET_EXACT = {
    "/api/health",
    "/api/archives",
    "/api/archives/check",
    "/api/features",
    "/api/settings",
    "/api/docs",
    "/api/docs/page",
    "/api/summary",
    "/api/timeline",
    "/api/dates/sources",
    "/api/map/clusters",
    "/api/map/points",
    "/api/map/cluster/merge-preview",
    "/api/edit-log",
    "/api/merge-targets",
    "/api/faces/summary",
    "/api/pets/summary",
    "/api/pets",
    "/api/pet/detections",
    "/api/nonhuman",
    "/api/faces/persons",
    "/api/faces/suggestions",
    "/api/dups/summary",
    "/api/dups",
    "/api/media",
    "/api/browse/filters",
    "/api/folders",
    "/api/browse/semantic/status",
    "/api/browse/semantic/search",
    "/api/browse/text/status",
    "/api/browse/text/search",
    # Added with the rebuilt media viewer (c4bce51) and never declared here, so
    # this guard had been failing since. It is declared rather than covered:
    # nothing in the suite drives ``routes/search.py::similar`` yet, and this
    # file's job is to say what the surface *is*, not to pretend otherwise.
    "/api/similar",
    "/api/pipeline",
    "/",
    "/index.html",
    "/manifest.webmanifest",
    "/sw.js",
}
# GET, prefix (12: 4 /api + 8 non-api).
GET_PREFIX = {
    "/api/map/cluster/",
    "/api/pet/",
    "/api/faces/person/",
    "/api/item/",
    "/icon-",
    "/static/",
    "/vendor/",
    "/archivethumb/",
    "/thumb/",
    "/faceThumb/",
    "/animalThumb/",
    "/file/",
}
# POST, exact (28, no prefixes).
POST_EXACT = {
    "/api/archives",
    "/api/archive/configure",
    "/api/archive/open",
    "/api/archive/close",
    "/api/archive/remove",
    "/api/pipeline/pause",
    "/api/pipeline/changed",
    "/api/map/cluster/rename",
    "/api/map/cluster/merge",
    "/api/map/cluster/unmerge",
    "/api/edit-log/undo",
    "/api/faces/person/cover",
    "/api/faces/person/rename",
    "/api/faces/reassign",
    "/api/faces/merge",
    "/api/faces/unmerge",
    "/api/faces/detach",
    "/api/faces/different",
    "/api/faces/skip",
    "/api/faces/hide",
    "/api/faces/unhide",
    "/api/pet/cover",
    "/api/pet/hide",
    "/api/pet/unhide",
    "/api/pet/detach",
    "/api/pet/rename",
    "/api/pets/merge",
    "/api/pets/unmerge",
    "/api/nonhuman/review",
    "/api/item/date",
    "/api/item/place",
    "/api/item/person/add",
    "/api/item/person/remove",
    "/api/item/pet/add",
    "/api/item/pet/remove",
    "/api/places/create",
}


def _route_literals(source: str) -> set[str]:
    """Every path string literal a `path` condition compares against. Line-scoped
    (not a whole-file regex) so it only picks up literals that are actually part
    of a route condition."""
    literals: set[str] = set()
    for line in source.splitlines():
        if "path ==" in line or "path in (" in line or "path.startswith(" in line:
            literals.update(re.findall(r'"([^"]*)"', line))
    return literals


def test_the_route_tables_are_exactly_the_routes_these_tests_cover():
    """The tables must equal GET_EXACT/GET_PREFIX/POST_EXACT above, so a route
    added or dropped without a matching update here fails a test instead of
    silently drifting out of what this file covers.

    Verified to fail on a planted regression: dropping ``"/api/health"`` from
    GET_EXACT, or removing an entry from ``routes.GET_ROUTES``, each trips this
    assertion (checked by hand -- there is no supported way to mutate the
    shipped server from within a test)."""
    get_table = set(server.routes.GET_ROUTES) | {p for p, _ in server.routes.GET_PREFIX_ROUTES}

    assert get_table == GET_EXACT | GET_PREFIX
    assert set(server.routes.POST_ROUTES) == POST_EXACT


def test_server_py_routes_nothing_by_hand():
    """``server.py`` must contain no path condition at all.

    It used to hold two if-elif chains, and this guard existed to check them
    against the tables while routes moved across a commit at a time. Nothing is
    left to check -- which is exactly why the guard is kept and inverted. A
    hand-rolled ``if path == ...`` added beside the tables would work, so
    nothing else would complain, and it would be invisible to both the drift
    check above and the generated ``docs/dev/api.md``. Registering it in a table
    is the only supported way to add a route, and this is what says so.
    """
    stray = _route_literals(Path(server.__file__).read_text())
    assert not stray, f"server.py routes these by hand instead of via a table: {sorted(stray)}"
