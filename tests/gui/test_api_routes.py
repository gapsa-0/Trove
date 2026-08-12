"""One test per HTTP route, plus the dispatcher's failure modes.

This file was written to protect a rewrite: ``do_GET``/``do_POST`` were two
long if-elif chains covering 66 routes (39 GET, 27 POST), almost none of whose
branches the suite ever executed, and a route silently dropped or mistyped
while they became route tables would have failed nothing. That rewrite has
landed -- the routes now live in ``trove/web/routes/`` and the
chains are gone -- and these tests are what says it changed no behaviour.

The trap this file is built around: an unknown path and a legitimate
"no such record" answer return the *same* body (``{"error": "not found"}``,
404), one from the dispatcher's fall-through and one from a handler that
looked and found nothing. So "not 404" proves nothing. Every test here
instead spins up a real archive with real rows (``live_server`` below) and
asserts a GET returns 200 with the JSON shape that route actually produces,
or a POST returns its real success body -- not merely "didn't fall through".

Static asset and media-serving routes (no JSON shape to check) instead
assert 200 and a sane content-type. The one static case with a genuinely
fixed body (``/api/settings``) gets an exact-equality test.

Every case here is a single request. The routes that only mean anything as a
sequence -- undo, unmerge, restore, and the destructive ones -- live in
``test_api_sequences.py``; the drift guard that checks these lists against the
route tables lives in ``test_route_tables.py``.

The live server and its seeded archive live in ``live_archive.py``; the
dispatcher's own failure modes (unknown paths, CORS, traversal) live in
``test_http_dispatch.py``.
"""

from __future__ import annotations

import json

import pytest
from helpers import needs_embedding
from live_archive import _get, _post

# ---------------------------------------------------------------------------
# GET -- /api routes (29: 25 exact + 4 prefix)
# ---------------------------------------------------------------------------

API_GET_CASES = [
    pytest.param("/api/health", {"ok", "version", "commit"}, id="GET /api/health"),
    pytest.param("/api/archives", {"archives"}, id="GET /api/archives"),
    # Asked of the archive's own folder, which is the answer the picker acts on
    # and the one no other case can disturb: the unregistered directory beside
    # it is what POST /api/archives registers, so a case using that would pass
    # or fail depending on which ran first.
    pytest.param(
        "/api/archives/check?path={archive_path}",
        {"error", "archive_id"},
        id="GET /api/archives/check",
    ),
    pytest.param("/api/features", {"features"}, id="GET /api/features"),
    pytest.param(
        "/api/edit-log?root={root_id}&entity=person&id={person_a}",
        {"entries"},
        id="GET /api/edit-log",
    ),
    pytest.param(
        "/api/summary?root={root_id}",
        {"total", "size", "types", "with_gps", "enriched", "date_min", "date_max"},
        id="GET /api/summary",
    ),
    pytest.param("/api/timeline?root={root_id}", {"bucket", "series"}, id="GET /api/timeline"),
    pytest.param(
        "/api/dates/sources?root={root_id}",
        {"total", "dated", "undated", "sources"},
        id="GET /api/dates/sources",
    ),
    pytest.param(
        "/api/map/clusters?root={root_id}", {"clusters", "hidden"}, id="GET /api/map/clusters"
    ),
    pytest.param(
        "/api/map/points?root={root_id}", {"points", "unplaced"}, id="GET /api/map/points"
    ),
    pytest.param(
        "/api/map/cluster/merge-preview?root={root_id}&a={place_a}&b={place_b}",
        {"ok", "span_km", "threshold_km", "warn"},
        id="GET /api/map/cluster/merge-preview (exact)",
    ),
    pytest.param(
        "/api/map/cluster/{place_a}?root={root_id}",
        {"id", "name", "lat", "lon", "total", "members", "offset", "count", "merges"},
        id="GET /api/map/cluster/ (prefix)",
    ),
    pytest.param(
        "/api/faces/summary?root={root_id}",
        {
            "total_images",
            "scanned",
            "unscanned",
            "faces",
            "people",
            "photos_with_faces",
            "backend_available",
        },
        id="GET /api/faces/summary",
    ),
    pytest.param(
        "/api/pets/summary?root={root_id}",
        {
            "total_images",
            "scanned",
            "unscanned",
            "detections",
            "pets",
            "nonhuman_faces",
            "backend_available",
        },
        id="GET /api/pets/summary",
    ),
    pytest.param("/api/pets?root={root_id}", {"pets", "offset", "count"}, id="GET /api/pets"),
    pytest.param(
        "/api/pet/detections?root={root_id}",
        {"items", "offset", "count"},
        id="GET /api/pet/detections",
    ),
    pytest.param(
        "/api/nonhuman?root={root_id}",
        {"items", "total", "offset", "count"},
        id="GET /api/nonhuman",
    ),
    pytest.param(
        "/api/pet/{pet_a}?root={root_id}",
        {"id", "name", "species", "photos", "items", "offset", "count", "merges"},
        id="GET /api/pet/ (prefix)",
    ),
    pytest.param(
        "/api/faces/persons?root={root_id}",
        {"people", "offset", "count"},
        id="GET /api/faces/persons",
    ),
    pytest.param(
        "/api/faces/suggestions?root={root_id}",
        {"suggestions"},
        id="GET /api/faces/suggestions",
    ),
    pytest.param(
        "/api/faces/person/{person_a}?root={root_id}",
        {"id", "name", "photos", "items", "offset", "count", "merges"},
        id="GET /api/faces/person/ (prefix)",
    ),
    pytest.param(
        "/api/dups/summary?root={root_id}",
        {"groups", "duplicates", "reclaimable", "by_match", "by_media"},
        id="GET /api/dups/summary",
    ),
    pytest.param("/api/dups?root={root_id}", {"groups", "offset", "count"}, id="GET /api/dups"),
    pytest.param(
        "/api/media?root={root_id}",
        {"items", "offset", "limit", "count", "total"},
        id="GET /api/media",
    ),
    pytest.param(
        "/api/browse/filters?root={root_id}",
        {"periods", "types", "people", "places", "indexed_any", "located_any"},
        id="GET /api/browse/filters",
    ),
    pytest.param("/api/folders?root={root_id}", {"folders"}, id="GET /api/folders"),
    pytest.param(
        "/api/browse/semantic/status?root={root_id}",
        {"total", "indexed", "skipped", "errors", "pending", "by_type", "configured", "enabled"},
        id="GET /api/browse/semantic/status",
    ),
    pytest.param(
        "/api/browse/semantic/search?root={root_id}&q=beach",
        {"items", "offset", "limit", "count", "total"},
        # One route, not the file: this handler embeds the typed query, so it
        # refuses with ModelUnavailableError unless the whole semantic stack is
        # present -- while its forty-odd neighbours run fine without it.
        marks=needs_embedding,
        id="GET /api/browse/semantic/search",
    ),
    pytest.param(
        "/api/browse/text/status?root={root_id}",
        {
            "total",
            "read",
            "by_type",
            "skipped",
            "errors",
            "pending",
            "passages",
            "configured",
            "enabled",
        },
        id="GET /api/browse/text/status",
    ),
    pytest.param(
        "/api/browse/text/search?root={root_id}&q=contrato",
        {"items", "offset", "limit", "count", "total"},
        # No mark: searching text needs SQLite and nothing else, which is the
        # difference between this half of Browse and the one above it.
        id="GET /api/browse/text/search",
    ),
    pytest.param(
        "/api/item/{plain}",
        {
            "id",
            "name",
            "type",
            "people",
            "animals",
            "place",
            "place_options",
            "person_options",
            "pet_options",
        },
        id="GET /api/item/ (prefix)",
    ),
    pytest.param(
        "/api/pipeline?root={root_id}",
        {"root_id", "overall", "stages", "extra", "paused", "paused_stages"},
        id="GET /api/pipeline",
    ),
]


@pytest.mark.parametrize("path_template, expected_keys", API_GET_CASES)
def test_every_get_api_route_answers_with_its_shape(live_server, path_template, expected_keys):
    path = path_template.format(**live_server.ids)
    status, _content_type, body = _get(live_server.base_url, path)
    assert status == 200, body
    payload = json.loads(body)
    assert expected_keys <= payload.keys(), payload


def test_semantic_status_is_not_configured_on_an_archive_that_declined_it(live_server):
    """``configured`` has to mean "this archive will have something to search",
    which takes the feature choice as well as the importable model.

    It used to report only the latter, so an archive set up without Search by
    description was told the feature was configured — and the Browse screen
    then offered a composer over an index whose stage the scheduler leaves out
    of the pipeline entirely, above a line promising files "queued for
    indexing" that nothing would ever index.
    """
    base, root_id = live_server.base_url, live_server.ids["root_id"]
    path = f"/api/browse/semantic/status?root={root_id}"

    status, _ct, body = _get(base, path)
    assert status == 200, body
    assert json.loads(body)["enabled"] is True

    ok, body = _post(base, "/api/archive/configure", {"root_id": root_id, "features": ["places"]})
    assert ok == 200, body

    status, _ct, body = _get(base, path)
    assert status == 200, body
    payload = json.loads(body)
    assert payload["enabled"] is False
    assert payload["configured"] is False


def test_get_settings_returns_the_constant_empty_object(live_server):
    """The one route with no shape worth checking by subset -- it always
    returns exactly ``{}`` -- so this gets an exact-equality test instead of
    a slot in API_GET_CASES."""
    status, _content_type, body = _get(live_server.base_url, "/api/settings")
    assert status == 200, body
    assert json.loads(body) == {}


# ---------------------------------------------------------------------------
# POST -- /api routes, one single-shot case each. The rest are sequences,
# in test_api_sequences.py.
# ---------------------------------------------------------------------------

API_POST_CASES = [
    pytest.param(
        "/api/archives",
        lambda ids: {"path": ids["extra_archive_path"]},
        {"id", "path", "name", "features"},
        id="POST /api/archives",
    ),
    pytest.param(
        "/api/archive/configure",
        lambda ids: {"root_id": ids["root_id"], "name": "Configured"},
        {"ok", "name", "features"},
        id="POST /api/archive/configure",
    ),
    pytest.param(
        "/api/archive/open",
        lambda ids: {"root_id": ids["root_id"]},
        {"ok"},
        id="POST /api/archive/open",
    ),
    pytest.param(
        "/api/archive/close",
        lambda ids: {"root_id": ids["root_id"]},
        {"ok"},
        id="POST /api/archive/close",
    ),
    pytest.param(
        "/api/pipeline/pause",
        lambda ids: {"paused": True},
        {"paused"},
        id="POST /api/pipeline/pause (whole)",
    ),
    pytest.param(
        "/api/map/cluster/rename",
        lambda ids: {"cluster_id": ids["place_a"], "name": "Home Renamed"},
        {"ok", "name"},
        id="POST /api/map/cluster/rename",
    ),
    pytest.param(
        "/api/faces/person/rename",
        lambda ids: {"person_id": ids["person_a"], "name": "Ana Renamed"},
        {"ok", "name"},
        id="POST /api/faces/person/rename",
    ),
    pytest.param(
        "/api/faces/reassign",
        lambda ids: {"face_id": ids["face_a"], "person_id": ids["person_b"]},
        {"ok", "person"},
        id="POST /api/faces/reassign",
    ),
    pytest.param(
        "/api/faces/detach",
        lambda ids: {"person_id": ids["person_a"], "file_id": ids["person_a_photo"]},
        {"ok", "detached_faces"},
        id="POST /api/faces/detach",
    ),
    pytest.param(
        "/api/faces/different",
        lambda ids: {"a": ids["person_a"], "b": ids["person_b"]},
        {"ok"},
        id="POST /api/faces/different",
    ),
    pytest.param(
        "/api/faces/skip",
        lambda ids: {"a": ids["person_a"], "b": ids["person_b"]},
        {"ok"},
        id="POST /api/faces/skip",
    ),
    pytest.param(
        "/api/faces/hide",
        lambda ids: {"person_id": ids["person_a"]},
        {"ok"},
        id="POST /api/faces/hide",
    ),
    pytest.param(
        "/api/faces/person/cover",
        lambda ids: {"person_id": ids["person_a"], "face_id": ids["face_a"]},
        {"ok", "cover_face_id"},
        id="POST /api/faces/person/cover",
    ),
    pytest.param(  # the reversible kind: this hides, the next restores
        "/api/faces/hide",
        lambda ids: {"person_id": ids["person_b"], "reason": "unknown"},
        {"ok"},
        id="POST /api/faces/hide (unknown)",
    ),
    pytest.param(
        "/api/faces/unhide",
        lambda ids: {"person_id": ids["person_b"]},
        {"ok"},
        id="POST /api/faces/unhide",
    ),
    pytest.param(
        "/api/pet/rename",
        lambda ids: {"pet_id": ids["pet_a"], "name": "Rocco Renamed"},
        {"ok", "name"},
        id="POST /api/pet/rename",
    ),
    pytest.param(
        "/api/nonhuman/review",
        lambda ids: {"detection_id": ids["nonhuman"], "verdict": "confirmed"},
        {"ok", "status", "root_id"},
        id="POST /api/nonhuman/review",
    ),
    pytest.param(
        "/api/item/date",
        lambda ids: {"file_id": ids["plain"], "datetime": "2023-05"},
        {"ok", "date", "date_source"},
        id="POST /api/item/date",
    ),
    pytest.param(
        "/api/item/place",
        lambda ids: {"file_id": ids["plain"], "place_id": ids["place_a"]},
        {"ok", "place"},
        id="POST /api/item/place (set)",
    ),
    pytest.param(
        "/api/item/person/add",
        lambda ids: {"person_id": ids["person_a"], "file_id": ids["plain"]},
        {"ok", "person"},
        id="POST /api/item/person/add",
    ),
    pytest.param(
        "/api/item/person/remove",
        lambda ids: {"person_id": ids["person_a"], "file_id": ids["plain"]},
        {"ok"},
        id="POST /api/item/person/remove",
    ),
    pytest.param(
        "/api/item/pet/add",
        lambda ids: {"pet_id": ids["pet_a"], "file_id": ids["plain"]},
        {"ok", "pet"},
        id="POST /api/item/pet/add",
    ),
    pytest.param(
        "/api/item/pet/remove",
        lambda ids: {"pet_id": ids["pet_a"], "file_id": ids["plain"]},
        {"ok"},
        id="POST /api/item/pet/remove",
    ),
    pytest.param(
        "/api/places/create",
        lambda ids: {"root": ids["root_id"], "name": "New Place", "lat": 1.0, "lon": 2.0},
        {"ok", "id", "place"},
        id="POST /api/places/create",
    ),
]


@pytest.mark.parametrize("path, body_fn, expected_keys", API_POST_CASES)
def test_every_post_api_route_answers_with_its_success_shape(
    live_server, path, body_fn, expected_keys
):
    status, body = _post(live_server.base_url, path, body_fn(live_server.ids))
    payload = json.loads(body)
    assert status == 200, payload
    assert expected_keys <= payload.keys(), payload
