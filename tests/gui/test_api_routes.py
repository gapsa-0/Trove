"""One test per HTTP route, plus the dispatcher's failure modes.

This file was written to protect a rewrite: ``do_GET``/``do_POST`` were two
long if-elif chains covering 66 routes (39 GET, 27 POST), almost none of whose
branches the suite ever executed, and a route silently dropped or mistyped
while they became route tables would have failed nothing. That rewrite has
landed -- the routes now live in ``organize_archive/web/routes/`` and the
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

A drift guard at the bottom checks the route tables against the literal lists
these tests declare, so an added-and-forgotten or silently dropped route fails
a test here. It still also parses ``server.py`` for ``path ==`` /
``path in (...)`` / ``path.startswith(...)`` conditions even though there are
none left: that half now guards against a hand-rolled branch reappearing
beside the tables, which would be a route the generated API docs never see.

The live server and its seeded archive live in ``live_archive.py``; the
dispatcher's own failure modes (unknown paths, CORS, traversal) live in
``test_http_dispatch.py``.
"""

from __future__ import annotations

import json

import pytest
from live_archive import _get, _post

# ---------------------------------------------------------------------------
# GET -- /api routes (28: 24 exact + 4 prefix)
# ---------------------------------------------------------------------------

API_GET_CASES = [
    pytest.param("/api/health", {"ok", "version", "commit"}, id="GET /api/health"),
    pytest.param("/api/archives", {"archives"}, id="GET /api/archives"),
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
        {"total", "indexed", "skipped", "errors", "pending", "by_type", "configured"},
        id="GET /api/browse/semantic/status",
    ),
    pytest.param(
        "/api/browse/semantic/search?root={root_id}&q=beach",
        {"items", "offset", "limit", "count", "total"},
        id="GET /api/browse/semantic/search",
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


def test_get_settings_returns_the_constant_empty_object(live_server):
    """The one route with no shape worth checking by subset -- it always
    returns exactly ``{}`` -- so this gets an exact-equality test instead of
    a slot in API_GET_CASES."""
    status, _content_type, body = _get(live_server.base_url, "/api/settings")
    assert status == 200, body
    assert json.loads(body) == {}


# ---------------------------------------------------------------------------
# POST -- /api routes (19 single-shot cases; 8 more as dedicated tests below
# for routes that mutate destructively or need a multi-request sequence)
# ---------------------------------------------------------------------------

API_POST_CASES = [
    pytest.param(
        "/api/archives",
        lambda ids: {"path": ids["extra_archive_path"]},
        {"id", "path"},
        id="POST /api/archives",
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


# ---------------------------------------------------------------------------
# POST -- dedicated tests: destructive routes, and routes that need a
# multi-request sequence to exercise for real.
# ---------------------------------------------------------------------------


def test_post_archive_remove_deletes_the_registered_archive(live_server):
    ids = live_server.ids
    base = live_server.base_url
    status, body = _post(base, "/api/archive/remove", {"root_id": ids["root_id"]})
    payload = json.loads(body)
    assert status == 200, payload
    assert payload.keys() >= {"ok", "path"}

    # Not just a plausible-looking response: the archive is actually gone.
    _status, _ct, body = _get(base, "/api/archives")
    assert json.loads(body)["archives"] == []


def test_post_pipeline_pause_supports_pausing_a_single_stage(live_server):
    status, body = _post(
        live_server.base_url, "/api/pipeline/pause", {"paused": True, "stage": "scan"}
    )
    payload = json.loads(body)
    assert status == 200, payload
    assert payload.keys() >= {"paused", "paused_stages"}
    assert "scan" in payload["paused_stages"]


def test_post_item_place_clears_an_assignment(live_server):
    """Exercises the ``clear`` branch of /api/item/place; the "set" branch is
    already covered by API_POST_CASES."""
    ids = live_server.ids
    base = live_server.base_url
    status, body = _post(
        base, "/api/item/place", {"file_id": ids["plain"], "place_id": ids["place_a"]}
    )
    assert json.loads(body).get("ok") is True, body

    status, body = _post(base, "/api/item/place", {"file_id": ids["plain"], "clear": True})
    payload = json.loads(body)
    assert status == 200, payload
    assert payload == {"ok": True, "place": None}


def test_post_faces_merge_then_unmerge_round_trips(live_server):
    """merge_persons doesn't hand back the merge_id unmerge needs -- the real
    GUI reads it off the survivor's ``merges`` list (face_person's "merges"
    key) after the merge, exactly like this test does -- so this is the only
    way to exercise /api/faces/unmerge against a genuine merge_id, and
    incidentally the only way to exercise /api/faces/merge with two already
    differently-named clusters (it refuses that without an explicit ``name``,
    see merge_persons' docstring)."""
    ids = live_server.ids
    base = live_server.base_url

    status, body = _post(
        base,
        "/api/faces/merge",
        {"a": ids["person_a"], "b": ids["person_b"], "name": "Merged Person"},
    )
    payload = json.loads(body)
    assert status == 200, payload
    survivor_id = payload["person"]["id"]

    _status, _ct, body = _get(base, f"/api/faces/person/{survivor_id}?root={ids['root_id']}")
    merges = json.loads(body)["merges"]
    assert merges, "expected the merge just made to be listed as undoable"
    merge_id = merges[0]["id"]

    status, body = _post(base, "/api/faces/unmerge", {"merge_id": merge_id})
    payload = json.loads(body)
    assert status == 200, payload
    assert payload == {"ok": True, "recluster": True}


def test_post_pets_merge_then_unmerge_round_trips(live_server):
    """Mirrors test_post_faces_merge_then_unmerge_round_trips; see its
    docstring for why this is a two-request sequence."""
    ids = live_server.ids
    base = live_server.base_url

    status, body = _post(
        base, "/api/pets/merge", {"a": ids["pet_a"], "b": ids["pet_b"], "name": "Merged Pet"}
    )
    payload = json.loads(body)
    assert status == 200, payload
    survivor_id = payload["pet"]["id"]

    _status, _ct, body = _get(base, f"/api/pet/{survivor_id}?root={ids['root_id']}")
    merges = json.loads(body)["merges"]
    assert merges, "expected the merge just made to be listed as undoable"
    merge_id = merges[0]["id"]

    status, body = _post(base, "/api/pets/unmerge", {"merge_id": merge_id})
    payload = json.loads(body)
    assert status == 200, payload
    assert payload == {"ok": True, "recluster": True}


def test_post_map_cluster_merge_then_unmerge_round_trips(live_server):
    """Mirrors test_post_faces_merge_then_unmerge_round_trips. Places are
    durable (place_merges' schema comment), so unlike the face/pet unmerge
    the response carries no ``recluster`` flag -- unmerge_place_clusters is
    already a complete restore, see its docstring."""
    ids = live_server.ids
    base = live_server.base_url

    status, body = _post(
        base,
        "/api/map/cluster/merge",
        {"a": ids["place_a"], "b": ids["place_b"], "name": "Merged Place"},
    )
    payload = json.loads(body)
    assert status == 200, payload
    survivor_id = payload["place"]["id"]

    _status, _ct, body = _get(base, f"/api/map/cluster/{survivor_id}?root={ids['root_id']}")
    merges = json.loads(body)["merges"]
    assert merges, "expected the merge just made to be listed as undoable"
    merge_id = merges[0]["id"]

    status, body = _post(base, "/api/map/cluster/unmerge", {"merge_id": merge_id})
    payload = json.loads(body)
    assert status == 200, payload
    assert payload.keys() == {"ok", "place_id"}
    assert payload["ok"] is True
    assert isinstance(payload["place_id"], int)
