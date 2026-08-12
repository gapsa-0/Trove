"""The API calls that only mean anything as a sequence.

Split from ``test_api_routes.py``, which holds one single-shot case per route:
send this body, get that shape. These cannot be written that way. Undoing a
merge needs a merge_id that only exists once a merge has happened; undoing an
edit needs an entry id that is only knowable by asking for it afterwards;
restoring a hidden group needs a group that has been hidden. Each is a real
two- or three-request conversation, which is also the only way the route is
ever reached in the app.

The destructive ones live here too (removing an archive, pausing a stage), for
the older reason that a parametrised case which deletes its own fixture makes
every case after it depend on ordering.
"""

from __future__ import annotations

import json

from live_archive import _get, _post


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


def test_post_pipeline_changed_is_accepted_as_a_hint(live_server):
    """The app sends this when its window comes back to the front, on the
    chance that files were dropped in while it was away. It claims nothing and
    is not believed on anything -- the pipeline re-walks and decides -- so the
    only contract is that it is accepted and answers at once."""
    root_id = live_server.ids["root_id"]

    status, body = _post(live_server.base_url, f"/api/pipeline/changed?root={root_id}", {})

    assert status == 200, body
    assert json.loads(body) == {"ok": True}


def test_post_pipeline_changed_needs_to_know_which_archive(live_server):
    status, body = _post(live_server.base_url, "/api/pipeline/changed", {})

    assert status == 400, body


def test_post_pipeline_pause_supports_pausing_a_single_stage(live_server):
    """Every card the health panel draws a pause button for, not just one of
    them: the panel offers the button per card, so a card the route refuses is
    a button that reports failure when pressed."""
    from trove.pipeline import stages

    for card in stages.CARD_ORDER:
        status, body = _post(
            live_server.base_url, "/api/pipeline/pause", {"paused": True, "stage": card}
        )
        payload = json.loads(body)
        assert status == 200, (card, payload)
        assert payload.keys() >= {"paused", "paused_stages"}
        assert card in payload["paused_stages"]


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


def test_post_edit_log_undo_round_trips_a_rename(live_server):
    """Like unmerge's merge_id, an entry id is only knowable by asking after the
    edit -- and a rename is the action whose reversal the log performs itself."""
    ids, base = live_server.ids, live_server.base_url
    log = f"/api/edit-log?root={ids['root_id']}&entity=person&id={ids['person_a']}"
    entries = lambda: json.loads(_get(base, log)[2])["entries"]  # noqa: E731 - read four times

    status, body = _post(
        base, "/api/faces/person/rename", {"person_id": ids["person_a"], "name": "Renamed Once"}
    )
    assert status == 200, body
    first = entries()[0]
    assert (first["action"], first["undoable"]) == ("rename", True)

    status, body = _post(base, "/api/edit-log/undo", {"entry_id": first["id"]})
    assert status == 200, body
    assert json.loads(body).get("ok") is True

    undone = [e for e in entries() if e["id"] == first["id"]]
    assert undone and undone[0]["undone"] is True, "the entry should read as undone, not vanish"


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
