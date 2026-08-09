"""A browser that already holds a thumbnail is not made to fetch it again.

Media answered with no ``Cache-Control``, no ``ETag`` and no ``Last-Modified``
-- nothing a cache can act on -- so Chrome refetched every tile on every
render. That is the whole grid on each screen change, and the whole filmstrip
on each arrow press, against a server that speaks HTTP/1.0 and therefore opens
a fresh connection per image. Each of those requests also *regenerates* what
it asks for when the disk cache has nothing: two ffmpeg processes for a video,
a Pillow decode for a photo.
"""

from __future__ import annotations

from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from live_archive import _get

MEDIA_PATHS = [
    pytest.param("/thumb/{plain}", id="a grid thumbnail"),
    pytest.param("/faceThumb/{face_a}", id="a face crop"),
    pytest.param("/animalThumb/{detection_a}", id="a pet crop"),
    pytest.param("/file/{plain}", id="the original file"),
]


def _fetch(base_url: str, path: str, headers: dict | None = None):
    req = Request(f"{base_url}{path}", headers=headers or {})
    try:
        with urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


@pytest.mark.parametrize("template", MEDIA_PATHS)
def test_a_media_response_says_how_long_it_may_be_kept_and_what_it_is(live_server, template):
    path = template.format(**live_server.ids)

    status, headers, body = _fetch(live_server.base_url, path)

    assert status == 200, body
    assert headers.get("Cache-Control"), f"{path} gives a cache nothing to go on"
    assert headers.get("ETag"), f"{path} gives a cache nothing to revalidate with"


@pytest.mark.parametrize("template", MEDIA_PATHS)
def test_asking_again_with_the_tag_costs_no_body(live_server, template):
    path = template.format(**live_server.ids)
    _, headers, _first = _fetch(live_server.base_url, path)
    etag = headers["ETag"]

    status, again, body = _fetch(live_server.base_url, path, {"If-None-Match": etag})

    assert status == 304, f"resent {len(body)} bytes the client already had"
    assert body == b""
    assert "Content-Length" not in again, "304 must not claim a body length"
    assert again.get("ETag") == etag


def test_a_browser_returning_the_tag_as_weak_is_still_answered_304(live_server):
    """Browsers revalidate with ``W/"..."`` even for a tag they were given
    strong. Missing that match means resending the file, which is the exact
    cost this is here to avoid."""
    path = f"/thumb/{live_server.ids['plain']}"
    _, headers, _ = _fetch(live_server.base_url, path)

    status, _, _ = _fetch(live_server.base_url, path, {"If-None-Match": f"W/{headers['ETag']}"})

    assert status == 304


def test_a_stale_tag_is_answered_with_the_file(live_server):
    path = f"/thumb/{live_server.ids['plain']}"

    status, _, body = _fetch(live_server.base_url, path, {"If-None-Match": '"not-this-one"'})

    assert status == 200
    assert body


def test_two_different_files_do_not_share_a_tag(live_server):
    """A validator that collides is worse than none: the second file would
    never be fetched at all."""
    ids = live_server.ids
    _, one, _ = _fetch(live_server.base_url, f"/thumb/{ids['plain']}")
    _, other, _ = _fetch(live_server.base_url, f"/thumb/{ids['person_a_photo']}")

    assert one["ETag"] != other["ETag"]


def test_a_range_request_still_works(live_server):
    """Video seeking rides on Range, and the tag check runs before it."""
    req = Request(
        f"{live_server.base_url}/file/{live_server.ids['plain']}", headers={"Range": "bytes=0-9"}
    )
    with urlopen(req, timeout=10) as resp:
        assert resp.status == 206
        assert len(resp.read()) == 10
        assert resp.headers.get("ETag")


def test_the_app_shell_is_still_never_cached(live_server):
    """The shell and its modules deliberately answer no-store, so a reload
    picks up a server update. Media caching must not have reached them."""
    for path in ("/", "/static/js/main.js"):
        _, headers, _ = _fetch(live_server.base_url, path)
        assert headers.get("Cache-Control") == "no-store", path


def test_a_missing_file_is_still_a_404(live_server):
    status, _, _ = _get(live_server.base_url, "/thumb/999999")
    assert status == 404
