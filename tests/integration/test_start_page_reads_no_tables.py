"""The start page must not read a files table to draw a card.

This ran before anything appeared on screen, for every registered archive, with
the page cache cold -- the state the app opens in. One of its three aggregates
summed `sha256 IS NOT NULL`, a column in no index and a number nothing ever
displayed, so drawing the start page meant pulling all 263 MB of a 97k-file
archive off disk: 738 ms of empty page.

Asserted through the query plan rather than a stopwatch. A timing test on a
fast machine passes at 25 ms whether or not the table was read; SCAN over
`files` is the thing that was wrong, and it is the thing that stays out.
"""

from trove.config import Config
from trove.db import database as db
from trove.services import archives

# The query archives() runs per archive, kept here as the shape under test.
STATS = "SELECT COUNT(*) c, COALESCE(SUM(size),0) s FROM files f WHERE f.present = 1"
COVERS = """SELECT f.id FROM files f WHERE f.present=1 AND f.hidden=0
            AND f.media_type='image' ORDER BY f.id DESC LIMIT 5"""


def _archive(monkeypatch, tmp_path, files=200):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    cfg = Config.load()
    folder = tmp_path / "photos"
    folder.mkdir()
    added = archives.add_archive(cfg, str(folder))
    conn = db.connect(cfg.archive_db_path(added["id"]))
    conn.executemany(
        """INSERT INTO files(root_id, rel_path, ext, size, mtime, media_type,
                             sha256, first_seen, last_seen)
           VALUES(?,?,'jpg',?,0,'image',?,'now','now')""",
        [(added["id"], f"{n}.jpg", 1000 + n, f"{n:064x}") for n in range(files)],
    )
    conn.commit()
    conn.close()
    return cfg, added["id"]


def _plan(cfg, archive_id, sql):
    conn = db.open_readonly(cfg.archive_db_path(archive_id))
    try:
        return " ".join(r["detail"] for r in conn.execute("EXPLAIN QUERY PLAN " + sql))
    finally:
        conn.close()


def test_the_card_counts_are_answered_without_reading_the_table(monkeypatch, tmp_path):
    """COVERING is the word that matters, and the reason this is asserted on
    the plan and not on a stopwatch.

    "No table scan" is not the property: without idx_files_size SQLite still
    finds the rows through idx_files_browse and then fetches every one of them
    for `size`, and says so as SEARCH ... USING INDEX -- which is what 714 ms
    looked like. COVERING INDEX is the promise that the table is never opened.
    """
    cfg, archive_id = _archive(monkeypatch, tmp_path)

    plan = _plan(cfg, archive_id, STATS)

    assert "COVERING INDEX idx_files_size" in plan, plan


def test_the_cover_ids_are_answered_from_an_index(monkeypatch, tmp_path):
    cfg, archive_id = _archive(monkeypatch, tmp_path)

    plan = _plan(cfg, archive_id, COVERS)

    assert "SCAN files" not in plan, plan


def test_the_start_page_asks_for_nothing_it_does_not_show(monkeypatch, tmp_path):
    """`hashed` was computed for every archive on every load and read by
    nothing -- not the cards, not the summary line, not the desktop shell. The
    keys here are what the start page actually draws with."""
    cfg, _ = _archive(monkeypatch, tmp_path)

    row = archives.archives(cfg)[0]

    assert set(row) == {
        "id",
        "path",
        "name",
        "features",
        "added_at",
        "files",
        "size",
        "exists",
        "last_scan",
        "covers",
    }
    assert row["files"] == 200
    assert row["size"] == sum(1000 + n for n in range(200))
