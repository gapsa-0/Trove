"""A file still being written must not be catalogued half-copied.

The scanner records a file's size and a hash of its bytes. Run that against a
file a copy is still filling and what gets stored describes a fragment: the
wrong size, a hash of half a video, and — because a hash that changed means the
content changed — every derived row cleared and rebuilt from the fragment too.

Two checks, because one cannot do it. Before reading, a file written moments ago
is left alone, which is what stops a 40 GB read that is going to be thrown away.
After reading, the file is stat'd again, which is what actually makes it correct:
no window can be wide enough for a copy that runs for minutes, and only the
second look knows whether the bytes just read still describe the file.
"""

from __future__ import annotations

import os
import time

from trove.config import Config
from trove.db import database as db
from trove.scan import walker


def _archive(tmp_path):
    conn = db.connect(tmp_path / "archive.db")
    db.init_db(conn)
    db.reconcile_root(conn, 1, str(tmp_path / "photos"))
    (tmp_path / "photos").mkdir()
    return conn


def _aged(path, seconds=60, data=b"settled"):
    """Write a file and backdate it: one that finished arriving a while ago."""
    path.write_bytes(data)
    old = time.time() - seconds
    os.utime(path, (old, old))
    return path


# -- the two checks, on their own --------------------------------------------


def test_a_file_written_a_moment_ago_reads_as_still_arriving(tmp_path):
    f = tmp_path / "now.jpg"
    f.write_bytes(b"x")

    assert walker._arriving_now(f.stat()) is True


def test_a_file_that_has_settled_does_not(tmp_path):
    f = _aged(tmp_path / "old.jpg", seconds=walker.SETTLE_SECONDS + 5)

    assert walker._arriving_now(f.stat()) is False


def test_a_file_dated_in_the_future_is_not_mistaken_for_one_still_arriving(tmp_path):
    """A camera with the wrong date writes files stamped years ahead. Reading
    "newer than now" as "still being written" would mean never cataloguing them
    at all, which is why the check is on the distance and not the direction."""
    f = tmp_path / "future.jpg"
    f.write_bytes(b"x")
    ahead = time.time() + 86_400 * 365
    os.utime(f, (ahead, ahead))

    assert walker._arriving_now(f.stat()) is False


def test_a_file_that_grew_while_we_read_it_is_still_arriving(tmp_path):
    """The half that catches a large video: no window would have helped, and
    the second stat costs nothing."""
    f = tmp_path / "video.mp4"
    f.write_bytes(b"first")
    before = f.stat()
    f.write_bytes(b"first and then some more")

    assert walker._still_arriving(f, before) is True


def test_a_file_that_sat_still_while_we_read_it_is_not(tmp_path):
    f = _aged(tmp_path / "video.mp4")
    before = f.stat()

    assert walker._still_arriving(f, before) is False


def test_a_file_that_vanished_while_we_read_it_counts_as_arriving(tmp_path):
    f = tmp_path / "gone.jpg"
    f.write_bytes(b"x")
    before = f.stat()
    f.unlink()

    assert walker._still_arriving(f, before) is True


# -- what a scan does with them ----------------------------------------------


def test_a_file_still_being_written_is_left_out_and_counted(tmp_path, monkeypatch):
    conn = _archive(tmp_path)
    _aged(tmp_path / "photos" / "settled.jpg")
    (tmp_path / "photos" / "copying.mp4").write_bytes(b"half")
    # Stays mid-copy however long the run waits, which a real 40 GB copy does
    # too and a test cannot without sitting there for it.
    monkeypatch.setattr(walker, "_still_arriving", lambda path, before: path.name.endswith(".mp4"))

    stats = walker.scan_root(conn, Config(), str(tmp_path / "photos"), db.now_iso(), root_id=1)

    assert stats.unstable == 1
    rows = [r[0] for r in conn.execute("SELECT rel_path FROM files")]
    assert rows == ["settled.jpg"]


def test_a_photo_that_lands_mid_walk_is_picked_up_by_the_same_run(tmp_path):
    """The case this must not make slower. A photograph copied in a moment
    before the scan reaches it is inside the settle window, so the walk passes
    it by -- and if that were the end of the run it would wait for the next
    scan, which is deliberately held off for half a minute. The run comes back
    to it instead.
    """
    conn = _archive(tmp_path)
    (tmp_path / "photos" / "just-arrived.jpg").write_bytes(b"fresh")

    stats = walker.scan_root(conn, Config(), str(tmp_path / "photos"), db.now_iso(), root_id=1)

    assert stats.unstable == 0
    assert stats.new == 1
    assert [r[0] for r in conn.execute("SELECT rel_path FROM files")] == ["just-arrived.jpg"]


def test_a_file_already_in_the_catalogue_does_not_vanish_while_it_is_rewritten(
    tmp_path, monkeypatch
):
    """Skipping a file must not be mistaken for the file being gone.

    ``scan_root`` finishes by marking everything it did not touch as missing, so
    a file being copied over one already catalogued would disappear from the
    library for as long as the copy took -- the row is still there and still
    correct, it is only the new bytes that are not readable yet.
    """
    conn = _archive(tmp_path)
    settled = _aged(tmp_path / "photos" / "holiday.jpg", data=b"original")
    walker.scan_root(conn, Config(), str(tmp_path / "photos"), db.now_iso(), root_id=1)
    assert conn.execute("SELECT present FROM files").fetchone()[0] == 1

    settled.write_bytes(b"being replaced right now")
    monkeypatch.setattr(walker, "_still_arriving", lambda path, before: True)
    stats = walker.scan_root(conn, Config(), str(tmp_path / "photos"), db.now_iso(), root_id=1)

    assert stats.unstable == 1
    assert conn.execute("SELECT present FROM files").fetchone()[0] == 1
