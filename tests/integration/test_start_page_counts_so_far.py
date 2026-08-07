"""A start-page card says "so far" until the archive has been walked through.

The card counts what has been catalogued, and an archive still being walked for
the first time has more of both figures to come -- so a bare "12,040 files ·
12 GB" on one halfway through reads as the size of the folder when it is a floor.

The question is "has a scan ever finished here", asked through
``db.last_completed_scan`` -- the same function ``scan_settled`` asks it with,
which is the point. It is emphatically not "is the newest scan_runs row open".
Those two come apart and then stay apart: backing out of an archive cancels its
scan and leaves an open row behind, and an archive that is already covered never
queues another scan to replace it, so that row stays the newest one for good. A
97k-file archive catalogued days earlier read as "so far" permanently on exactly
that, while the pipeline had it settled and was three stages further on.

The other way an archive is short of its folder is a run that walked past a file
still being copied. That run finished, so its row is closed; ``files_unstable``
is what says it covered nothing of the sort.
"""

from trove.config import Config
from trove.db import database as db
from trove.scan.walker import ScanStats
from trove.services import archives


def _archive(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    cfg = Config.load()
    folder = tmp_path / "photos"
    folder.mkdir()
    return cfg, archives.add_archive(cfg, str(folder))["id"]


def _partial(cfg, archive_id):
    return next(a for a in archives.archives(cfg) if a["id"] == archive_id)["partial"]


def _finished(conn, archive_id, seen=200, unstable=0):
    run = db.scan_run_start(conn, archive_id, ["/photos"])
    db.scan_run_finish(conn, run, ScanStats(seen=seen, unstable=unstable), files_on_disk=seen)
    return run


def test_an_archive_that_has_never_scanned_counts_so_far(monkeypatch, tmp_path):
    """Its zero is "nothing yet", not "this folder is empty"."""
    cfg, archive_id = _archive(monkeypatch, tmp_path)

    assert _partial(cfg, archive_id) is True


def test_a_first_scan_still_walking_counts_so_far(monkeypatch, tmp_path):
    cfg, archive_id = _archive(monkeypatch, tmp_path)
    conn = db.connect(cfg.archive_db_path(archive_id))
    db.scan_run_start(conn, archive_id, ["/photos"])
    conn.close()

    assert _partial(cfg, archive_id) is True


def test_a_first_scan_that_was_interrupted_counts_so_far(monkeypatch, tmp_path):
    """Nothing is running and the archive has still never been walked through,
    which is the state the qualifier is for."""
    cfg, archive_id = _archive(monkeypatch, tmp_path)
    conn = db.connect(cfg.archive_db_path(archive_id))
    db.scan_run_start(conn, archive_id, ["/photos"])  # killed before it finished
    conn.close()

    assert _partial(cfg, archive_id) is True


def test_a_finished_scan_is_the_whole_archive(monkeypatch, tmp_path):
    cfg, archive_id = _archive(monkeypatch, tmp_path)
    conn = db.connect(cfg.archive_db_path(archive_id))
    _finished(conn, archive_id)
    conn.close()

    assert _partial(cfg, archive_id) is False


def test_a_cancelled_rescan_does_not_undo_a_finished_one(monkeypatch, tmp_path):
    """The bug this file exists to hold shut.

    Backing out of an archive cancels its scan, leaving an open row that is now
    the newest one -- and because the archive is already covered, no later scan
    is ever queued to replace it. Reading "newest row is open" as "still being
    read" is therefore not a state that resolves; it is permanent, and it was
    permanent on a 97k-file archive that had been complete for days.
    """
    cfg, archive_id = _archive(monkeypatch, tmp_path)
    conn = db.connect(cfg.archive_db_path(archive_id))
    _finished(conn, archive_id)
    db.scan_run_start(conn, archive_id, ["/photos"])
    db.scan_run_start(conn, archive_id, ["/photos"])
    conn.close()

    assert _partial(cfg, archive_id) is False


def test_a_run_that_left_a_file_still_copying_counts_so_far(monkeypatch, tmp_path):
    """It walked the whole tree and covered less than all of it, which is the
    same thing scan_settled refuses to settle on."""
    cfg, archive_id = _archive(monkeypatch, tmp_path)
    conn = db.connect(cfg.archive_db_path(archive_id))
    _finished(conn, archive_id, unstable=1)
    conn.close()

    assert _partial(cfg, archive_id) is True


def test_a_database_this_build_has_never_migrated_still_draws_a_card(monkeypatch, tmp_path):
    """The start page is the one place that reads an archive without opening it.

    Opening is what runs init_db, so on the first launch after an upgrade every
    registered archive is still on the previous schema -- and the start page
    reads all of them, read-only, before anything has had a chance to migrate.
    A column added in this version is therefore not there yet, and asking for it
    unguarded fails the whole page for every archive at once rather than for
    some edge case later.
    """
    cfg, archive_id = _archive(monkeypatch, tmp_path)
    conn = db.connect(cfg.archive_db_path(archive_id))
    _finished(conn, archive_id)
    conn.execute("ALTER TABLE scan_runs DROP COLUMN files_unstable")
    conn.commit()
    conn.close()

    assert _partial(cfg, archive_id) is False


def test_the_card_settles_once_that_file_has_landed(monkeypatch, tmp_path):
    cfg, archive_id = _archive(monkeypatch, tmp_path)
    conn = db.connect(cfg.archive_db_path(archive_id))
    _finished(conn, archive_id, unstable=1)
    assert _partial(cfg, archive_id) is True

    _finished(conn, archive_id, seen=201, unstable=0)
    conn.close()

    assert _partial(cfg, archive_id) is False
