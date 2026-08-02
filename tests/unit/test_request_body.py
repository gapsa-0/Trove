"""The JSON body is whatever the client posted, and must be checked as such.

Two fields -- ``set_date``'s ``datetime`` and ``create_place``'s ``name`` --
used to be handed to a service declaring ``str`` behind a ``cast``. The cast
was a promise this boundary could not keep: ``{"datetime": 5}`` is a valid
JSON document, and the service's ``(value or "").strip()`` then raised
``AttributeError`` inside a handler, which the app answers with a 500 and a
traceback. A malformed request is a 400.

These tests exercise ``Request.body_str`` directly rather than over HTTP: the
mapping from ``ValueError`` to 400 is one place in the dispatcher and is
covered by ``tests/gui/test_api_routes.py``. What is worth pinning here is
which values the helper accepts, which it rejects, and that "optional" did not
quietly become "anything".
"""

from __future__ import annotations

import pytest

from organize_archive.web.routes._request import Request


def _req(body: dict) -> Request:
    """A Request carrying only a body -- no cfg or jobs needed to read it."""
    return Request(
        method="POST",
        path="/api/x",
        query={},
        body=body,
        cfg=None,
        jobs=None,  # type: ignore[arg-type]
    )


def test_a_string_field_is_returned_as_is():
    assert _req({"datetime": "2022-05-14"}).body_str("datetime") == "2022-05-14"


def test_an_empty_string_is_a_legitimate_value():
    """Distinct from absent: clearing a place's name posts "", and that must
    not be confused with not sending the field."""
    assert _req({"name": ""}).body_str("name", default="unnamed") == ""


@pytest.mark.parametrize("value", [5, 5.5, True, ["2022"], {"y": 2022}])
def test_a_present_value_of_the_wrong_type_is_rejected(value):
    """Including when a default is offered -- optional is not "anything"."""
    with pytest.raises(ValueError, match="datetime must be a string"):
        _req({"datetime": value}).body_str("datetime")
    with pytest.raises(ValueError, match="datetime must be a string"):
        _req({"datetime": value}).body_str("datetime", default="")


def test_an_absent_field_falls_back_to_the_default():
    """This is what keeps the fix from changing any working path: a client
    that omits the field still reaches the service with the same value it
    used to get, and the service's own error message still applies."""
    assert _req({}).body_str("name", default="") == ""


def test_an_absent_field_with_no_default_is_rejected():
    with pytest.raises(ValueError, match="name must be a string"):
        _req({}).body_str("name")


def test_an_explicit_null_is_treated_as_absent():
    """`{"name": null}` is how JSON spells "no value", so it takes the default
    rather than failing the type check."""
    assert _req({"name": None}).body_str("name", default="") == ""
