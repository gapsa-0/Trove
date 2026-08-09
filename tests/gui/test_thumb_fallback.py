"""What /thumb/ answers when no thumbnail could be made.

The route used to fall back to the original file for anything that was not a
known video or a PDF, which reads as "the browser can probably cope". It
cannot: an ``<img>`` pointed at a .docx, a camera RAW or a backup .zip
downloads the whole thing and then fails to decode it. The viewer's filmstrip
asks for a thumbnail of every neighbour in the gallery, so a 29 GB archive
file sitting next to a photo was a 29 GB response into an ``<img>`` -- and
with six connections per origin, everything else on the page waited behind it.

The fallback is still worth having for the formats it was written for: a JPEG
whose last bytes are missing is refused by the decoder here and rendered
perfectly well by the browser. So it is an allowlist now, not a blacklist.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import factories
import pytest
from live_archive import _get

from trove.db import database as db


def _catalogue(live_server) -> sqlite3.Connection:
    return db.connect(live_server.cfg.archive_db_path(live_server.ids["root_id"]))


def _add(live_server, rel_path: str, media_type: str, body: bytes) -> int:
    """A real file on disk plus its catalogue row, in the open archive."""
    path = Path(live_server.ids["archive_path"]) / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    conn = _catalogue(live_server)
    try:
        fid = factories.add_file(
            conn,
            root_id=live_server.ids["root_id"],
            rel_path=rel_path,
            media_type=media_type,
            size=len(body),
        )
        conn.commit()
    finally:
        conn.close()
    return fid


# Files no decoder here can read, one per way of being undecodable. Each is
# real enough to exist on disk and be catalogued; none can become a thumbnail.
UNRENDERABLE = [
    pytest.param("notes.docx", "document", id="a word processor document"),
    pytest.param("sheet.csv", "document", id="a spreadsheet export"),
    pytest.param("page.html", "document", id="a saved web page"),
    pytest.param("shot.cr2", "image", id="a camera raw file"),
    pytest.param("shot.dng", "image", id="a digital negative"),
    pytest.param("backup.zip", "archive", id="an archive file"),
    pytest.param("clip.mts", "video", id="a camcorder clip"),
    pytest.param("voice.opus", "audio", id="a voice note"),
]


@pytest.mark.parametrize("rel_path, media_type", UNRENDERABLE)
def test_a_file_no_thumbnail_can_be_made_of_is_absent_not_downloaded(
    live_server, rel_path, media_type
):
    body = b"\x00\x01\x02\x03" * 4096
    fid = _add(live_server, rel_path, media_type, body)

    status, content_type, served = _get(live_server.base_url, f"/thumb/{fid}")

    assert status == 404, (
        f"{rel_path} answered {status} {content_type} with {len(served)} bytes; "
        "an <img> cannot decode it, and the tile's own fallback icon is what this is for"
    )
    assert served != body


def test_a_photo_the_decoder_refuses_is_still_offered_to_the_browser(live_server):
    """The one case the fallback exists for. Browsers render a JPEG whose last
    bytes are missing; Pillow refuses it. Sending the original is how the tile
    shows a picture anyway."""
    from PIL import Image

    whole = Path(live_server.ids["archive_path"]) / "whole.jpg"
    Image.new("RGB", (640, 480), (200, 90, 40)).save(whole, "JPEG")
    truncated = whole.read_bytes()[:-400]
    fid = _add(live_server, "cut-short.jpg", "image", truncated)

    status, content_type, served = _get(live_server.base_url, f"/thumb/{fid}")

    assert status == 200, served
    assert content_type.startswith("image/")


def test_an_ordinary_photo_still_gets_a_generated_thumbnail(live_server):
    """The allowlist governs the fallback only -- a file that thumbnails
    normally must not start answering with its own bytes."""
    from PIL import Image

    path = Path(live_server.ids["archive_path"]) / "big.jpg"
    Image.new("RGB", (2000, 1500), (30, 120, 200)).save(path, "JPEG")
    fid = _add(live_server, "big.jpg", "image", path.read_bytes())

    status, content_type, served = _get(live_server.base_url, f"/thumb/{fid}")

    assert status == 200
    assert content_type.startswith("image/")
    assert len(served) < path.stat().st_size, "served the original instead of a thumbnail"
