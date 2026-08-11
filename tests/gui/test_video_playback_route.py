"""``/file/<id>?play=1``: the same id, re-encoded into something playable.

The route it hangs off already had one job -- hand back the file's own bytes,
Range and all -- and this is deliberately a query on it rather than a route of
its own: it is the same file, answered differently, and the viewer swaps one
URL for the other on the same element.

What separates the two answers is worth pinning, because the failure is silent
in both directions. A re-encoding served with the original's caching would put
a per-request body in the browser's cache under the id of the file itself; the
original served with the re-encoding's framing would lose Range, and with it
seeking in every video that never needed re-encoding at all.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import factories
import pytest
from live_archive import _get, _get_ranged

from trove.media import transcode
from trove.runtime import no_window, tool, tool_env

needs_ffmpeg = pytest.mark.skipif(
    tool("ffmpeg") is None, reason="re-encoding for playback needs ffmpeg"
)


@pytest.fixture
def clip(live_server):
    """A real video in the live archive, catalogued the way a scan would."""
    src = Path(live_server.ids["archive_path"]) / "clip.mp4"
    subprocess.run(
        [
            str(tool("ffmpeg")),
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x120:rate=10:duration=4",
            "-pix_fmt",
            "yuv420p",
            str(src),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=60,
        env=tool_env(),
        check=True,
        **no_window(),
    )
    from trove.db import database as db

    conn = db.connect(live_server.cfg.archive_db_path(live_server.ids["root_id"]))
    try:
        fid = factories.add_file(
            conn,
            root_id=live_server.ids["root_id"],
            rel_path="clip.mp4",
            media_type="video",
            size=src.stat().st_size,
        )
        conn.commit()
    finally:
        conn.close()
    return fid


@needs_ffmpeg
def test_asking_to_play_gets_a_re_encoding_rather_than_the_file(live_server, clip):
    status, ctype, body = _get(live_server.base_url, f"/file/{clip}?play=1")

    assert status == 200
    assert ctype == "video/mp4"
    assert body[4:8] == b"ftyp", "not an MP4 at all"
    # Not the file itself: the point of the route is that these differ.
    assert body != Path(live_server.ids["archive_path"], "clip.mp4").read_bytes()


@needs_ffmpeg
def test_the_re_encoding_is_never_cached_under_the_id_of_the_file(live_server, clip):
    """It is made per request, it depends on ``t``, and it is not the bytes at
    that id. A cache entry for it would be handed back to a later plain
    ``/file/`` request for the original."""
    import urllib.request

    with urllib.request.urlopen(f"{live_server.base_url}/file/{clip}?play=1", timeout=20) as resp:
        converted = dict(resp.headers)
    with urllib.request.urlopen(f"{live_server.base_url}/file/{clip}", timeout=20) as resp:
        original = dict(resp.headers)

    assert converted.get("Cache-Control") == "no-store"
    # A length nobody knows yet must not be claimed, and a stream cannot honour
    # a Range -- saying otherwise invites a client to ask for one.
    assert "Content-Length" not in converted
    # ...while the file itself keeps both, so seeking a video that plays as
    # itself is untouched by any of this.
    assert original.get("Accept-Ranges") == "bytes"
    assert "Content-Length" in original


@needs_ffmpeg
def test_the_original_still_answers_a_range(live_server, clip):
    """The guard on the change above: ``?play=1`` shares a handler with the
    route every photo and every playable video is served by."""
    status, headers, body = _get_ranged(live_server.base_url, f"/file/{clip}", "bytes=0-99")

    assert status == 206
    assert len(body) == 100
    assert headers["Content-Range"].startswith("bytes 0-99/")


def test_a_machine_with_no_ffmpeg_is_told_so_rather_than_left_hanging(
    live_server, clip_id_only, monkeypatch
):
    """An install with no ffmpeg is supported. The viewer already knows from
    ``can_reencode`` and does not normally ask, so this is the window between
    the two -- and it has to be an answer, not a hung request."""
    monkeypatch.setattr(transcode, "tool", lambda name: None)

    status, _ctype, body = _get(live_server.base_url, f"/file/{clip_id_only}?play=1")

    assert status == 404
    # Named, not the dispatcher's generic 404: an unknown path, an id that is
    # not in the catalogue and this all answer 404, and a test that accepted
    # any of them would still pass with the branch deleted.
    assert b"cannot re-encode" in body, body


@pytest.fixture
def clip_id_only(live_server):
    """A catalogued video with no bytes behind it worth encoding: this test
    never reaches ffmpeg, and building a real clip would need the very thing it
    is pretending is missing."""
    from trove.db import database as db

    src = Path(live_server.ids["archive_path"]) / "nothing.avi"
    src.write_bytes(b"not a video")
    conn = db.connect(live_server.cfg.archive_db_path(live_server.ids["root_id"]))
    try:
        fid = factories.add_file(
            conn,
            root_id=live_server.ids["root_id"],
            rel_path="nothing.avi",
            media_type="video",
        )
        conn.commit()
    finally:
        conn.close()
    return fid


def test_the_item_payload_says_whether_re_encoding_is_possible(live_server, clip_id_only):
    """What the viewer branches its whole message on: with no ffmpeg it must
    not promise a conversion, and it must not offer one for a photograph."""
    import json
    import urllib.request

    root = live_server.ids["root_id"]

    def payload(fid):
        url = f"{live_server.base_url}/api/item/{fid}?root={root}"
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.load(resp)

    assert payload(clip_id_only)["can_reencode"] is (tool("ffmpeg") is not None)
    # Never for anything that is not a video, whatever is installed.
    assert payload(live_server.ids["plain"])["can_reencode"] is False
