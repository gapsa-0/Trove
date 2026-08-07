"""A run that skipped a file still being copied has not covered the archive.

``scan_settled`` compares the file count on disk with the one the last completed
run saw, which is a good test for files appearing and disappearing and no test
at all for this: a video that finishes copying changes no count. So the run that
walked past it while it was still growing would be the last word, and the
finished video would sit outside the catalogue until something unrelated moved
the count.

Recording the skip is what closes that. The other half is not asking again too
soon: the only way to act on "something is still arriving" is to walk the whole
tree, and a copy that runs for ten minutes would otherwise cost a walk every
tick for ten minutes.
"""

from __future__ import annotations

from trove.db import database as db


class _Stats:
    """What scan_run_finish reads off a run."""

    def __init__(self, unstable=0, seen=10):
        self.seen = seen
        self.new = seen
        self.updated = 0
        self.bytes_hashed = 0
        self.unstable = unstable


def _archive(tmp_path):
    conn = db.connect(tmp_path / "archive.db")
    db.init_db(conn)
    db.reconcile_root(conn, 1, "/photos")
    return conn


def _run(conn, unstable=0, on_disk=10):
    run = db.scan_run_start(conn, 1, ["/photos"])
    db.scan_run_finish(conn, run, _Stats(unstable=unstable), files_on_disk=on_disk)
    return run


def test_a_clean_run_settles_the_archive(tmp_path):
    conn = _archive(tmp_path)
    _run(conn)

    assert db.scan_settled(conn, 1, 10) is True


def test_a_run_that_skipped_an_arriving_file_does_not(tmp_path):
    """Even though the counts agree -- which is the whole point. The file was
    on disk and counted, and deliberately not catalogued."""
    conn = _archive(tmp_path)
    _run(conn, unstable=1)

    assert db.scan_settled(conn, 1, 10) is False


def test_the_archive_settles_once_a_later_run_finds_nothing_arriving(tmp_path):
    conn = _archive(tmp_path)
    _run(conn, unstable=1)
    _run(conn, unstable=0)

    assert db.scan_settled(conn, 1, 10) is True


def test_a_change_on_disk_still_unsettles_a_clean_run(tmp_path):
    """The count comparison this is built on has not moved."""
    conn = _archive(tmp_path)
    _run(conn, on_disk=10)

    assert db.scan_settled(conn, 1, 11) is False


# -- not asking again on every tick ------------------------------------------


def test_an_arriving_file_is_not_rechecked_immediately(tmp_path):
    conn = _archive(tmp_path)
    _run(conn, unstable=1)

    assert db.scan_awaiting_settle(conn, 1, cooldown=30.0) is True


def test_it_is_rechecked_once_the_delay_has_passed(tmp_path):
    conn = _archive(tmp_path)
    _run(conn, unstable=1)

    # A cooldown of zero is "however long ago that was, it was longer than
    # this", which is what the scheduler sees on the tick after the delay.
    assert db.scan_awaiting_settle(conn, 1, cooldown=0.0) is False


def test_a_clean_run_is_never_waiting_on_anything(tmp_path):
    conn = _archive(tmp_path)
    _run(conn, unstable=0)

    assert db.scan_awaiting_settle(conn, 1, cooldown=30.0) is False


def test_an_archive_that_has_never_scanned_is_not_waiting_either(tmp_path):
    """Nothing to come back for, and the scan is owed in the ordinary way."""
    conn = _archive(tmp_path)

    assert db.scan_awaiting_settle(conn, 1, cooldown=30.0) is False


def test_a_run_recorded_before_this_column_existed_reads_as_clean(tmp_path):
    """Migrated databases carry NULL, written by a scanner that catalogued
    whatever it found -- there is nothing it was waiting to come back for."""
    conn = _archive(tmp_path)
    _run(conn)
    conn.execute("UPDATE scan_runs SET files_unstable=NULL")
    conn.commit()

    assert db.scan_settled(conn, 1, 10) is True
    assert db.scan_awaiting_settle(conn, 1, cooldown=30.0) is False
