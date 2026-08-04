from __future__ import annotations

from pathlib import Path
from urllib.request import urlopen

from helpers import serve_in_thread

from trove import paths
from trove.cli import main
from trove.config import Config


def test_linux_app_data_path_uses_xdg_and_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(paths.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert paths.app_data_dir() == tmp_path / "xdg" / "trove"

    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.setattr(paths.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert paths.app_data_dir() == tmp_path / "home" / ".local" / "share" / "trove"


def test_windows_app_data_path_uses_localappdata(monkeypatch, tmp_path):
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert paths.app_data_dir() == tmp_path / "local" / "trove"


def test_first_run_config_is_empty_and_load_does_not_create_files(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    cfg = Config.load()

    assert cfg.roots == []
    assert cfg.db_path == str(tmp_path / "trove" / "archive.db")
    assert not (tmp_path / "trove").exists()


def test_ensure_dirs_creates_standard_layout(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    cfg = Config.load()
    cfg.ensure_dirs()

    base = tmp_path / "trove"
    # Only app-wide binaries (ML models, the app icon) live under the shared
    # cache dir now. Thumbnails are per-archive and created lazily under each
    # archive's own cache dir (see test_archive_removal.py).
    assert (base / "cache" / "models").is_dir()
    assert (base / "cache" / "icons").is_dir()
    assert not (base / "cache" / "thumbs").exists()
    assert (base / "logs").is_dir()
    assert Path(cfg.db_path).parent.is_dir()


def test_gui_server_starts_cleanly_on_first_run(monkeypatch, tmp_path):
    """The welcome screen must be reachable before any archive exists.

    No archive means no database anywhere yet: each archive only gets its own
    database once it's added (see test_archive_removal.py), so unlike the old
    shared-catalog design, server startup has no single db_path to create.

    Uses the shared ``serve_in_thread`` lifecycle (tests/helpers.py) rather than
    the single ``handle_request`` this test used to hand-roll -- the assertions
    are unchanged, only how the server is stood up and torn down is now shared
    with test_api_routes.py.
    """
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "fresh-data"))
    cfg = Config.load()
    with serve_in_thread(cfg) as httpd:
        # Inside the server's lifetime on purpose: serve() calls ensure_dirs(),
        # and the claim is that *server startup* creates no database, not merely
        # that Config.load() doesn't.
        assert not Path(cfg.db_path).exists()
        host, port = httpd.server_address
        with urlopen(f"http://{host}:{port}/api/archives", timeout=2) as response:
            assert response.read() == b'{"archives": []}'


def test_db_override_is_not_saved_as_the_default(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "app-data"))
    override = tmp_path / "isolated" / "archive.db"

    assert main(["--db", str(override), "init"]) == 0
    assert override.exists()
    assert Config.load().db_path == str(tmp_path / "app-data" / "trove" / "archive.db")


def test_migrate_data_copies_legacy_files_without_removing_source(monkeypatch, tmp_path, capsys):
    source = tmp_path / "legacy"
    cache_file = source / "cache" / "thumbs" / "one.jpg"
    cache_file.parent.mkdir(parents=True)
    (source / "config.json").write_text("{}")
    (source / "archive.db").write_bytes(b"database")
    cache_file.write_bytes(b"thumbnail")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "new-data"))

    assert main(["migrate-data", "--from", str(source)]) == 0
    target = tmp_path / "new-data" / "trove"
    assert (target / "config.json").read_text() == "{}"
    assert (target / "archive.db").read_bytes() == b"database"
    assert (target / "cache" / "thumbs" / "one.jpg").read_bytes() == b"thumbnail"
    assert cache_file.exists()
    assert "original was kept" in capsys.readouterr().out


def test_migrate_data_rejects_occupied_target_and_unknown_source(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "new-data"))
    unknown = tmp_path / "unknown"
    unknown.mkdir()
    assert main(["migrate-data", "--from", str(unknown)]) == 1
    assert "contains none" in capsys.readouterr().err

    source = tmp_path / "legacy"
    source.mkdir()
    (source / "archive.db").write_bytes(b"database")
    target = tmp_path / "new-data" / "trove"
    target.mkdir(parents=True)
    (target / "archive.db").write_bytes(b"existing")
    assert main(["migrate-data", "--from", str(source)]) == 1
    assert "already contains" in capsys.readouterr().err


def test_migrate_legacy_archive_preserves_a_pre_isolation_catalog(monkeypatch, tmp_path):
    """An install from before per-archive isolation had one shared archive.db
    with a single root. It must keep showing up in the GUI, not vanish, once
    this version starts for the first time."""
    from trove.db import database as db

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    cfg = Config.load()
    cfg.ensure_dirs()

    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    root_id = db.get_or_create_root(conn, str(tmp_path / "source"))
    conn.execute(
        """INSERT INTO files(root_id, rel_path, size, mtime, media_type, sha256,
                             first_seen, last_seen)
           VALUES(?, 'a.jpg', 1, 0, 'image', 'x', 'now', 'now')""",
        (root_id,),
    )
    conn.commit()
    conn.close()
    thumb = Path(cfg.cache_dir) / "thumbs" / "x_v1_320.jpg"
    thumb.parent.mkdir(parents=True)
    thumb.write_bytes(b"thumb")

    cfg.migrate_legacy_archive()

    assert len(cfg.archives) == 1
    entry = cfg.archives[0]
    assert entry["path"] == str(tmp_path / "source")
    assert Path(cfg.archive_db_path(entry["id"])).is_file()
    assert (
        Path(cfg.archive_cache_dir(entry["id"])) / "thumbs" / "x_v1_320.jpg"
    ).read_bytes() == b"thumb"
    assert Path(cfg.db_path).is_file()  # the legacy file is copied, not moved

    # A later server start (fresh Config.load()) must not migrate a second time.
    cfg2 = Config.load()
    assert cfg2.legacy_migrated is True
    cfg2.migrate_legacy_archive()
    assert len(cfg2.archives) == 1


def test_migrate_legacy_archive_skips_multi_root_catalogs(monkeypatch, tmp_path, caplog):
    """A legacy catalog with several roots mixed their data (shared dedup
    groups, etc.) in ways that can't be split apart automatically — skip it
    rather than guess, and latch so it's not retried forever."""
    from trove.db import database as db

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    cfg = Config.load()
    cfg.ensure_dirs()

    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    db.get_or_create_root(conn, str(tmp_path / "one"))
    db.get_or_create_root(conn, str(tmp_path / "two"))
    conn.close()

    cfg.migrate_legacy_archive()

    assert cfg.archives == []
    assert cfg.legacy_migrated is True
    # Logged rather than printed: Config.load() runs inside the desktop backend
    # too, where stdout is only scanned for the READY handshake and discarded.
    assert "Each archive now needs its own database" in caplog.text
