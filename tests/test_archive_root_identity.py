"""An archive database's single root must always be the archive's registry id.

When those two drift apart the app fails quietly rather than loudly: the
scanner creates a second root for the same folder, every root-scoped query
returns almost nothing, and the scan stage never sees its backlog reach zero,
so the pipeline rescans forever. These cover both known ways in.
"""

from pathlib import Path

from organize_archive import paths
from organize_archive.config import Config
from organize_archive.db import database as db
from organize_archive.gui.queries import add_archive, remove_archive, summary


def _seed(conn, root_id, rel_path):
    return conn.execute(
        """INSERT INTO files(root_id, rel_path, size, mtime, media_type, sha256,
                             first_seen, last_seen)
           VALUES(?, ?, 1, 0, 'image', ?, 'now', 'now')""",
        (root_id, rel_path, rel_path),
    ).lastrowid


def test_reconcile_adopts_a_second_root_for_the_same_folder(tmp_path):
    """The symptom in the wild: a scan resolved its root by path, found the id
    taken by a stale folder, and filed 97k files under a root nobody queries."""
    conn = db.connect(tmp_path / "archive.db")
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id, path, added_at) VALUES(1, '/old', 'then')")
    conn.execute("INSERT INTO roots(id, path, added_at) VALUES(2, '/live', 'now')")
    _seed(conn, 1, "stale.jpg")
    _seed(conn, 2, "real.jpg")
    _seed(conn, 2, "real2.jpg")
    conn.commit()

    assert db.reconcile_root(conn, 1, "/live") is True

    assert [tuple(r) for r in conn.execute("SELECT id, path FROM roots")] == [(1, "/live")]
    # The scanned rows are adopted, keeping their ids (and everything hanging
    # off them); only the rows describing the folder that is no longer part of
    # this archive are dropped.
    assert [
        r["rel_path"]
        for r in conn.execute("SELECT rel_path FROM files WHERE root_id=1 ORDER BY rel_path")
    ] == ["real.jpg", "real2.jpg"]
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 2
    # Idempotent: a second pass is a no-op and reports nothing changed.
    assert db.reconcile_root(conn, 1, "/live") is False
    conn.close()


def test_reconcile_repoints_a_leftover_database_at_the_new_folder(tmp_path):
    """The other way in: an id reused after a removal, so the database on disk
    still describes a folder this archive has nothing to do with."""
    conn = db.connect(tmp_path / "archive.db")
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id, path, added_at) VALUES(1, '/old', 'then')")
    _seed(conn, 1, "old.jpg")
    conn.commit()

    assert db.reconcile_root(conn, 1, "/new") is True

    assert [tuple(r) for r in conn.execute("SELECT id, path FROM roots")] == [(1, "/new")]
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
    conn.close()


def test_reconcile_carries_root_scoped_side_tables(tmp_path):
    """Places and dedup coverage are keyed by root too; renumbering must take
    them along instead of orphaning them under an id that no longer exists."""
    conn = db.connect(tmp_path / "archive.db")
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id, path, added_at) VALUES(3, '/live', 'now')")
    _seed(conn, 3, "a.jpg")
    conn.execute("""INSERT INTO place_clusters(root_id, name, lat, lon, member_count,
                    created_at) VALUES(3, 'Home', 1.0, 2.0, 1, 'now')""")
    db.dedup_mark_done(conn, 3, 1, 1)
    conn.commit()

    db.reconcile_root(conn, 1, "/live")

    assert [r["root_id"] for r in conn.execute("SELECT root_id FROM place_clusters")] == [1]
    assert [r["root_id"] for r in conn.execute("SELECT root_id FROM dedup_runs")] == [1]
    conn.close()


def test_added_archive_is_queryable_under_its_registry_id(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    cfg = Config.load()
    src = tmp_path / "media"
    src.mkdir()

    aid = add_archive(cfg, str(src))["id"]
    conn = db.connect(cfg.archive_db_path(aid))
    _seed(conn, aid, "a.jpg")
    conn.commit()
    conn.close()

    assert summary(cfg.archive_db_path(aid), aid)["total"] == 1


def test_archive_ids_are_never_handed_out_twice(monkeypatch, tmp_path):
    """Removal deletes the archive directory on a best-effort basis. If that
    does not land, reusing the id would hand a new archive the old one's
    database — so ids only ever move forward."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    cfg = Config.load()
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()

    first = add_archive(cfg, str(one))["id"]
    # Simulate a removal whose directory cleanup failed: registry entry gone,
    # store still on disk.
    cfg.remove_archive_entry(first)
    assert paths.archive_dir(first).is_dir()

    second = add_archive(cfg, str(two))
    assert "error" not in second
    assert second["id"] != first

    conn = db.connect(cfg.archive_db_path(second["id"]))
    assert conn.execute("SELECT path FROM roots").fetchone()["path"] == str(two.resolve())
    conn.close()


def test_a_rejected_add_leaves_nothing_registered(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    cfg = Config.load()

    assert "error" in add_archive(cfg, str(tmp_path / "nope"))
    assert cfg.archives == []
    assert not paths.archives_dir().exists() or not any(paths.archives_dir().iterdir())


def test_removed_archive_still_leaves_the_rest_addressable(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    cfg = Config.load()
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    first = add_archive(cfg, str(one))["id"]
    second = add_archive(cfg, str(two))["id"]

    remove_archive(cfg, first)

    assert not paths.archive_dir(first).exists()
    conn = db.connect(cfg.archive_db_path(second))
    assert conn.execute("SELECT id FROM roots").fetchone()["id"] == second
    conn.close()
    assert Path(cfg.archive_db_path(second)).is_file()
