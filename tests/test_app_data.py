from __future__ import annotations

from pathlib import Path

from organize_archive import paths
from organize_archive.cli import main
from organize_archive.config import Config


def test_linux_app_data_path_uses_xdg_and_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(paths.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert paths.app_data_dir() == tmp_path / "xdg" / "organize_archive"

    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.setattr(paths.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert paths.app_data_dir() == tmp_path / "home" / ".local" / "share" / "organize_archive"


def test_windows_app_data_path_uses_localappdata(monkeypatch, tmp_path):
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert paths.app_data_dir() == tmp_path / "local" / "organize_archive"


def test_first_run_config_is_empty_and_load_does_not_create_files(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    cfg = Config.load()

    assert cfg.roots == []
    assert cfg.db_path == str(tmp_path / "organize_archive" / "archive.db")
    assert not (tmp_path / "organize_archive").exists()


def test_ensure_dirs_creates_standard_layout(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    cfg = Config.load()
    cfg.ensure_dirs()

    base = tmp_path / "organize_archive"
    assert (base / "cache" / "thumbs").is_dir()
    assert (base / "cache" / "models").is_dir()
    assert (base / "logs").is_dir()
    assert Path(cfg.db_path).parent.is_dir()


def test_db_override_is_not_saved_as_the_default(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "app-data"))
    override = tmp_path / "isolated" / "archive.db"

    assert main(["--db", str(override), "init"]) == 0
    assert override.exists()
    assert Config.load().db_path == str(tmp_path / "app-data" / "organize_archive" / "archive.db")


def test_migrate_data_copies_legacy_files_without_removing_source(monkeypatch, tmp_path, capsys):
    source = tmp_path / "legacy"
    cache_file = source / "cache" / "thumbs" / "one.jpg"
    cache_file.parent.mkdir(parents=True)
    (source / "config.json").write_text("{}")
    (source / "archive.db").write_bytes(b"database")
    cache_file.write_bytes(b"thumbnail")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "new-data"))

    assert main(["migrate-data", "--from", str(source)]) == 0
    target = tmp_path / "new-data" / "organize_archive"
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
    target = tmp_path / "new-data" / "organize_archive"
    target.mkdir(parents=True)
    (target / "archive.db").write_bytes(b"existing")
    assert main(["migrate-data", "--from", str(source)]) == 1
    assert "already contains" in capsys.readouterr().err
