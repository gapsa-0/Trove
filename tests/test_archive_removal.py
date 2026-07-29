from pathlib import Path

from organize_archive import paths
from organize_archive.config import Config
from organize_archive.db import database as db
from organize_archive.gui.queries import add_archive, remove_archive


def _seed_file(cfg, archive_id, rel_path, digest):
    conn = db.connect(cfg.archive_db_path(archive_id))
    try:
        db.init_db(conn)
        file_id = conn.execute(
            """INSERT INTO files(root_id, rel_path, size, mtime, media_type, sha256,
                                 first_seen, last_seen)
               VALUES(?, ?, 1, 0, 'image', ?, 'now', 'now')""",
            (archive_id, rel_path, digest),
        ).lastrowid
        conn.commit()
    finally:
        conn.close()
    return file_id


def test_remove_archive_only_touches_its_own_isolated_store(monkeypatch, tmp_path):
    """Each archive is its own database and cache dir now, so removing one
    must never reach into another — even one holding byte-identical content."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    cfg = Config.load()

    one_src, two_src = tmp_path / "one", tmp_path / "two"
    one_src.mkdir()
    two_src.mkdir()
    one = add_archive(cfg, str(one_src))["id"]
    two = add_archive(cfg, str(two_src))["id"]

    file_one = _seed_file(cfg, one, "one.jpg", "same-bytes-in-both")
    file_two = _seed_file(cfg, two, "two.jpg", "same-bytes-in-both")

    one_thumb = Path(cfg.archive_cache_dir(one)) / "thumbs" / f"fid{file_one}_v1_320.jpg"
    two_thumb = Path(cfg.archive_cache_dir(two)) / "thumbs" / f"fid{file_two}_v1_320.jpg"
    one_thumb.parent.mkdir(parents=True)
    two_thumb.parent.mkdir(parents=True)
    one_thumb.write_bytes(b"one")
    two_thumb.write_bytes(b"two")

    result = remove_archive(cfg, one)

    assert result == {"ok": True, "path": str(one_src.resolve())}
    assert not paths.archive_dir(one).exists()  # db + cache both gone
    assert [a["id"] for a in cfg.archives] == [two]

    # Archive two is completely untouched, despite the shared content hash.
    assert Path(cfg.archive_db_path(two)).is_file()
    assert two_thumb.exists()
    conn = db.connect(cfg.archive_db_path(two))
    try:
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
    finally:
        conn.close()


def test_remove_archive_reports_error_for_unknown_id(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    cfg = Config.load()
    assert remove_archive(cfg, 999) == {"error": "archive not found"}
