"""A start-page card says "so far" until a scan has finished.

The card counts what has been catalogued, and a scan still walking has more of
both figures to come -- so a bare "12,040 files · 12 GB" on an archive halfway
through its first scan reads as the size of the folder when it is a floor.

The whole answer is `scan_runs.finished_at`, which the catalog already keeps to
this exact meaning: `scan_run_finish` is reached only when every root was walked
end to end, so a scan that is running, was cancelled, or died leaves it NULL.
That is why an interrupted scan is honestly partial here and why the card does
not say "still scanning" -- nothing may be scanning at all.
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


def test_an_archive_that_has_never_scanned_counts_so_far(monkeypatch, tmp_path):
    """Its zero is "nothing yet", not "this folder is empty"."""
    cfg, archive_id = _archive(monkeypatch, tmp_path)

    assert _partial(cfg, archive_id) is True


def test_a_scan_still_walking_counts_so_far(monkeypatch, tmp_path):
    cfg, archive_id = _archive(monkeypatch, tmp_path)
    conn = db.connect(cfg.archive_db_path(archive_id))
    db.scan_run_start(conn, archive_id, ["/photos"])
    conn.close()

    assert _partial(cfg, archive_id) is True


def test_a_finished_scan_is_the_whole_archive(monkeypatch, tmp_path):
    cfg, archive_id = _archive(monkeypatch, tmp_path)
    conn = db.connect(cfg.archive_db_path(archive_id))
    run = db.scan_run_start(conn, archive_id, ["/photos"])
    db.scan_run_finish(conn, run, ScanStats(seen=200), files_on_disk=200)
    conn.close()

    assert _partial(cfg, archive_id) is False


def test_an_interrupted_scan_counts_so_far(monkeypatch, tmp_path):
    """The case a "still scanning" label would get wrong: this run is over, and
    it still never covered the tree, so the counts remain a floor."""
    cfg, archive_id = _archive(monkeypatch, tmp_path)
    conn = db.connect(cfg.archive_db_path(archive_id))
    run = db.scan_run_start(conn, archive_id, ["/photos"])
    db.scan_run_finish(conn, run, ScanStats(seen=200), files_on_disk=200)
    db.scan_run_start(conn, archive_id, ["/photos"])  # killed before it finished
    conn.close()

    assert _partial(cfg, archive_id) is True


def test_a_rescan_that_finishes_settles_the_card_again(monkeypatch, tmp_path):
    cfg, archive_id = _archive(monkeypatch, tmp_path)
    conn = db.connect(cfg.archive_db_path(archive_id))
    run = db.scan_run_start(conn, archive_id, ["/photos"])
    db.scan_run_finish(conn, run, ScanStats(seen=200), files_on_disk=200)
    again = db.scan_run_start(conn, archive_id, ["/photos"])
    assert _partial(cfg, archive_id) is True

    db.scan_run_finish(conn, again, ScanStats(seen=260), files_on_disk=260)
    conn.close()

    assert _partial(cfg, archive_id) is False
