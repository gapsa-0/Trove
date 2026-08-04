"""How the dispatcher behaves, separately from what each route returns.

Split out of ``test_api_routes.py``: that file answers "does every route work",
this one answers "what happens when the request is not a valid one" -- static
and media serving, unknown paths, the four error outcomes, cross-origin POSTs,
and path traversal.

The trap both files are built around: an unknown path and a legitimate "no such
record" answer return the *same* body (``{"error": "not found"}``, 404), one
from the dispatcher's fall-through and one from a handler that looked and found
nothing. So "not 404" proves nothing, and every test here asserts the specific
outcome rather than the absence of one.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.request import Request, urlopen

import pytest
from live_archive import _get, _get_ranged, _post

from trove.errors import ModelUnavailableError
from trove.web import server

# ---------------------------------------------------------------------------
# GET -- static assets (7 of the 12 non-/api routes)
# ---------------------------------------------------------------------------

STATIC_GET_CASES = [
    pytest.param("/", "text/html", id="GET / (exact)"),
    pytest.param("/index.html", "text/html", id="GET /index.html (exact)"),
    pytest.param(
        "/manifest.webmanifest", "application/manifest+json", id="GET /manifest.webmanifest (exact)"
    ),
    pytest.param("/sw.js", "text/javascript", id="GET /sw.js (exact)"),
    pytest.param("/icon-192.png", "image/png", id="GET /icon- (prefix)"),
    pytest.param("/vendor/leaflet.css", "text/css", id="GET /vendor/ (prefix)"),
    pytest.param("/static/css/base.css", "text/css", id="GET /static/ (prefix)"),
]


@pytest.mark.parametrize("path, content_type_prefix", STATIC_GET_CASES)
def test_every_static_get_route_answers_with_its_content_type(
    live_server, path, content_type_prefix
):
    status, content_type, body = _get(live_server.base_url, path)
    assert status == 200, body
    assert content_type.startswith(content_type_prefix), content_type
    assert body


# ---------------------------------------------------------------------------
# GET -- media-serving prefixes (5 of the 11 non-/api routes)
# ---------------------------------------------------------------------------


def test_get_thumb_serves_a_real_thumbnail(live_server):
    status, content_type, body = _get(live_server.base_url, f"/thumb/{live_server.ids['plain']}")
    assert status == 200, body
    assert content_type.startswith("image/")
    assert body


def test_get_archivethumb_serves_by_explicit_root_and_file_id(live_server):
    ids = live_server.ids
    status, content_type, body = _get(
        live_server.base_url, f"/archivethumb/{ids['root_id']}/{ids['plain']}"
    )
    assert status == 200, body
    assert content_type.startswith("image/")


def test_get_facethumb_serves_a_face_crop(live_server):
    status, content_type, body = _get(
        live_server.base_url, f"/faceThumb/{live_server.ids['face_a']}"
    )
    assert status == 200, body
    assert content_type.startswith("image/")


def test_get_animalthumb_serves_a_detection_crop(live_server):
    status, content_type, body = _get(
        live_server.base_url, f"/animalThumb/{live_server.ids['detection_a']}"
    )
    assert status == 200, body
    assert content_type.startswith("image/")


def test_get_file_serves_the_original_bytes(live_server):
    status, content_type, body = _get(live_server.base_url, f"/file/{live_server.ids['plain']}")
    assert status == 200, body
    assert content_type.startswith("image/")
    assert body


# ---------------------------------------------------------------------------
# GET -- range requests
#
# /file/<id> is what backs <video> and <audio> (static/js/item.js), so these
# are the requests a player makes: seeking issues a range, and some players
# probe the tail first to find an MP4's trailing `moov` atom. The parsing is
# pinned exhaustively in tests/unit/test_range_requests.py; what these three
# add is that the *response* is built from it correctly, which is where both
# of the bugs below actually showed.
# ---------------------------------------------------------------------------


def test_a_range_request_gets_that_slice_of_the_file(live_server):
    fid = live_server.ids["plain"]
    _status, _ctype, whole = _get(live_server.base_url, f"/file/{fid}")
    status, headers, body = _get_ranged(live_server.base_url, f"/file/{fid}", "bytes=0-9")
    assert status == 206, body
    assert headers["Content-Range"] == f"bytes 0-9/{len(whole)}"
    assert headers["Content-Length"] == "10"
    assert body == whole[:10]


def test_a_range_starting_past_the_end_is_refused_with_416(live_server):
    """It used to answer 206 with ``Content-Length: -4900`` and no body.

    A negative length is not a response a strict client has to tolerate, and
    416 is the answer that tells the player how long the file really is, so it
    can ask again for a range that exists.
    """
    fid = live_server.ids["plain"]
    _status, _ctype, whole = _get(live_server.base_url, f"/file/{fid}")
    status, headers, body = _get_ranged(live_server.base_url, f"/file/{fid}", "bytes=999999-")
    assert status == 416, body
    assert headers["Content-Range"] == f"bytes */{len(whole)}"
    assert int(headers["Content-Length"]) >= 0


def test_a_suffix_range_serves_the_tail_and_not_the_head(live_server):
    """``bytes=-20`` means the LAST 20 bytes.

    It used to be parsed as ``0-20`` and answered with the *first* 21, under a
    ``Content-Range`` that claimed exactly that -- so nothing about the reply
    looked wrong except the bytes in it. The assertion that matters is the
    last one; the header alone would have passed before the fix too.
    """
    fid = live_server.ids["plain"]
    _status, _ctype, whole = _get(live_server.base_url, f"/file/{fid}")
    status, headers, body = _get_ranged(live_server.base_url, f"/file/{fid}", "bytes=-20")
    assert status == 206, body
    assert headers["Content-Range"] == f"bytes {len(whole) - 20}-{len(whole) - 1}/{len(whole)}"
    assert body == whole[-20:]
    assert body != whole[:20]


def test_get_root_scoped_route_without_root_param_answers_400(live_server):
    """A root-scoped GET called without ``?root=`` answers 400, like every
    other validation failure in this file.

    This test used to assert the opposite, and was named
    ``..._drops_the_connection_silently``. Every GET route that resolves a
    ``root`` id and then asks for its database (summary, timeline,
    dates/sources, every /api/map/*, faces/summary, pets*, pet/*, nonhuman,
    faces/persons, faces/suggestions, faces/person/*, dups*, media, browse/*)
    raises ``ValueError("root is required")`` when the param is missing --
    and ``do_GET``'s outer ``except (ValueError, BrokenPipeError): pass``
    swallowed it having sent nothing at all, so the client saw
    ``RemoteDisconnected`` instead of a response. POST answered 400 for the
    same mistake the whole time; only GET had the hole.

    It is pinned on ``/api/summary``, but the fix is structural: both methods
    now answer through one dispatcher, so no route can reacquire this.
    """
    status, _content_type, body = _get(live_server.base_url, "/api/summary")
    assert status == 400, body
    assert json.loads(body) == {"error": "root is required"}


# ---------------------------------------------------------------------------
# The dispatcher's four outcomes. Everything above asks "does this route work";
# these ask "what happens when one doesn't", which is the half that used to
# have no coverage at all and where the silent-drop bug above lived.
#
# The failing handlers are injected into the live tables with monkeypatch: the
# server runs in a thread in this same process, so it looks up the same dict
# object these tests mutate. Faking the failure is the only way in -- no real
# route can be made to raise on demand without changing shipped code.
# ---------------------------------------------------------------------------


def test_an_unknown_get_path_answers_404(live_server):
    status, _content_type, body = _get(live_server.base_url, "/api/no-such-route")
    assert status == 404, body
    assert json.loads(body) == {"error": "not found"}


def test_an_unknown_post_path_answers_404(live_server):
    status, body = _post(live_server.base_url, "/api/no-such-route", {})
    assert status == 404, body
    assert json.loads(body) == {"error": "not found"}


def test_a_trove_error_answers_400_with_its_message(live_server, monkeypatch):
    """``TroveError`` is the "raised on purpose, message written for a person"
    base class, so its text goes to the client verbatim rather than becoming an
    anonymous 500."""

    def raiser(req):
        raise ModelUnavailableError("the face model is not installed")

    monkeypatch.setitem(server.routes.GET_ROUTES, "/api/health", raiser)
    status, _content_type, body = _get(live_server.base_url, "/api/health")
    assert status == 400, body
    assert json.loads(body) == {"error": "the face model is not installed"}


def test_an_unexpected_error_answers_500_and_logs_a_traceback(live_server, monkeypatch, caplog):
    """The other half of the same split: an error we did not raise on purpose
    is our bug, so it gets a traceback in the log. Before this stage a handler
    blowing up produced whatever ``BaseHTTPRequestHandler`` does by default and
    logged nothing."""

    def raiser(req):
        raise KeyError("cover_face_id")

    monkeypatch.setitem(server.routes.GET_ROUTES, "/api/health", raiser)
    with caplog.at_level(logging.ERROR, logger="trove.web.server"):
        status, _content_type, body = _get(live_server.base_url, "/api/health")

    assert status == 500, body
    assert "cover_face_id" in json.loads(body)["error"]
    assert any("Traceback" in rec.exc_text for rec in caplog.records if rec.exc_text)


# ---------------------------------------------------------------------------
# Hardening. This server reads a user's entire photo archive and listens on a
# predictable loopback port, so the two questions worth pinning are "can
# another site drive it" and "can a request name a file outside the archive".
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "origin",
    [
        pytest.param("http://evil.example", id="another site"),
        pytest.param("null", id="sandboxed iframe / file://"),
        # Same host, different port: a *different* local app, which is exactly
        # the neighbour a loopback-only check would wrongly trust.
        pytest.param("http://127.0.0.1:1", id="another local server"),
    ],
)
def test_a_cross_origin_post_is_refused(live_server, origin):
    """Loopback is not a security boundary: a tab on any website can POST to
    127.0.0.1 and the browser will send it.

    A JSON body would normally force a CORS preflight this server never
    answers -- but ``fetch`` with ``text/plain`` is a simple request that skips
    the preflight entirely, and ``_read_json_body`` parses the body whatever
    the content type claims. So the ``Origin`` check is what actually stops it.
    """
    status, body = _post(live_server.base_url, "/api/archive/close", {}, headers={"Origin": origin})
    assert status == 403, body
    assert json.loads(body) == {"error": "cross-origin request refused"}


def test_a_same_origin_post_is_allowed(live_server):
    """The other half, and the one that matters for not breaking the app: the
    GUI's own fetches carry an ``Origin`` matching ``Host``, and so does the
    Electron shell, which loads the app over http from this very server.

    ``base_url`` is exactly that origin -- scheme, host and port, no path --
    which is what the browser would send."""
    status, body = _post(
        live_server.base_url,
        "/api/archive/close",
        {},
        headers={"Origin": live_server.base_url},
    )
    assert status == 200, body
    assert json.loads(body) == {"ok": True}


def test_a_post_with_no_origin_header_is_allowed(live_server):
    """No ``Origin`` means the caller is not a browser -- curl, a script, this
    suite. Those are not the confused deputy the check defends against, and
    refusing them would break every non-browser client for no gain."""
    status, body = _post(live_server.base_url, "/api/archive/close", {})
    assert status == 200, body


def test_a_cross_origin_get_is_still_allowed(live_server):
    """GET is deliberately unchecked. It changes nothing, and a cross-origin
    caller cannot read the reply without the CORS headers this server never
    sends -- so refusing it would buy nothing and could break the shell."""
    req = Request(f"{live_server.base_url}/api/health", headers={"Origin": "http://evil.example"})
    with urlopen(req, timeout=5) as resp:
        assert resp.status == 200


# Deep enough to reach the filesystem root from web/vendor/ whatever the repo
# is checked out under. This is load-bearing: an earlier draft of these tests
# used three "..", which cannot climb out of the repo, so they passed against a
# deliberately broken server too and proved nothing. Any payload here must be
# able to actually reach /etc/passwd if the route lets it.
_UP = "../" * 12


def _payloads(prefix: str) -> list:
    """The same six payloads against either file-serving prefix."""
    return [
        pytest.param(f"{prefix}{_UP}etc/passwd", id=f"{prefix} dot-dot segments"),
        pytest.param(
            f"{prefix}{_UP.replace('/', '%2f')}etc%2fpasswd",
            id=f"{prefix} percent-encoded slashes",
        ),
        pytest.param(prefix + "....//" * 12 + "etc/passwd", id=f"{prefix} doubled dots"),
        pytest.param(f"{prefix}..", id=f"{prefix} bare dot-dot"),
        pytest.param(prefix, id=f"{prefix} empty name"),
        pytest.param(f"{prefix}{_UP}etc/passwd%00.css", id=f"{prefix} null byte"),
    ]


TRAVERSAL_PAYLOADS = _payloads("/vendor/") + _payloads("/static/css/")


@pytest.mark.parametrize("path", TRAVERSAL_PAYLOADS)
def test_the_file_serving_routes_cannot_escape_their_directory(live_server, path):
    """``/vendor/<name>`` and ``/static/<kind>/<name>`` are the only two routes
    that build a filesystem path out of the request rather than out of a
    database id, so between them they are this server's whole path-traversal
    surface.

    They are closed by different means, deliberately. ``/vendor/`` takes only
    the last ``/``-separated segment and refuses a segment containing ``..``;
    both are checked together because either alone would be enough today and
    neither is obviously permanent. ``/static/`` instead matches the whole
    suffix against ``(css|js)/[A-Za-z0-9_.-]+\\.(css|js)`` -- a pattern in which
    a traversal is not expressible, encoded or not, so there is no ordering of
    checks to get wrong.

    Verified non-vacuous by mutation, and the ``/vendor/`` result is worth
    recording. Breaking *only* the segment split (taking the whole path suffix
    instead) still passes -- the ``..`` refusal catches it. Breaking both makes
    the ``dot-dot segments`` case serve ``/etc/passwd`` with a 200, which this
    test then fails on. The other five payloads survive either mutation because
    nothing here percent-decodes, so they never become ``..`` at all. So the
    encoded variants document that the surface is closed rather than proving
    it, and the plain one is the case with teeth.

    ``/etc/passwd`` is the target because it exists on this machine; a payload
    aimed at a file that does not exist would 404 for the wrong reason.
    """
    assert Path("/etc/passwd").exists(), "this test's whole premise"
    status, _content_type, body = _get(live_server.base_url, path)
    assert status == 404, (status, body[:200])
    assert b"root:" not in body


def test_media_routes_take_an_id_not_a_path(live_server):
    """The counterpart to the test above, and why there are only two traversal
    surfaces: every thumbnail and original resolves through an integer id looked
    up in the database, so a path in the URL is never opened. A non-numeric id
    cannot reach the filesystem at all -- it fails to parse first.

    The status is asserted loosely (400 from the unparseable id, or 404 if the
    client library collapsed the ``..`` before sending) because which of the two
    arrives depends on urllib, not on this server. The assertion that matters is
    the second one, and it does not care which path got us there.
    """
    status, _content_type, body = _get(live_server.base_url, "/file/../../etc/passwd")
    assert status in (400, 404), (status, body)
    assert b"root:" not in body


def test_a_post_to_a_get_only_route_answers_404(live_server):
    """The two tables are independent, which is what lets ``/api/pet/`` be a GET
    prefix while ``/api/pet/rename`` is a POST. The flip side is that a method
    mismatch is a 404, not a 405 -- pinned because it is a real, deliberate
    consequence of the split rather than an oversight."""
    status, body = _post(live_server.base_url, "/api/summary", {})
    assert status == 404, body
