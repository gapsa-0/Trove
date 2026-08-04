"""The rename to Trove renamed the data directory, so an existing install's
catalogue has to be carried across. Getting this wrong does not raise -- it
silently presents someone's populated archive as a fresh, empty one, which is
the failure most likely to be mistaken for lost data."""

from __future__ import annotations

import json

import pytest

from trove.app_data_migration import migrate_legacy_app_data
from trove.paths import app_data_dir, legacy_app_data_dir


def _populate(directory, *, db_path=None, cache_dir=None):
    """Write the shape a pre-rename install would have left behind."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "archive.db").write_text("not really sqlite, but it is a file")
    (directory / "cache").mkdir(exist_ok=True)
    (directory / "cache" / "models").mkdir(exist_ok=True)
    (directory / "archives" / "1").mkdir(parents=True, exist_ok=True)
    (directory / "archives" / "1" / "archive.db").write_text("per-archive catalogue")
    config = {
        "roots": ["/home/someone/Photos"],
        "db_path": str(db_path if db_path is not None else directory / "archive.db"),
        "cache_dir": str(cache_dir if cache_dir is not None else directory / "cache"),
    }
    (directory / "config.json").write_text(json.dumps(config, indent=2))


def _config(directory) -> dict:
    return json.loads((directory / "config.json").read_text())


def test_a_pre_rename_directory_is_moved_across():
    legacy = legacy_app_data_dir()
    _populate(legacy)

    assert migrate_legacy_app_data() is True

    target = app_data_dir()
    assert (target / "archive.db").read_text() == "not really sqlite, but it is a file"
    assert (target / "archives" / "1" / "archive.db").is_file()
    assert (target / "cache" / "models").is_dir()
    assert not legacy.exists()


def test_the_moved_config_points_at_the_new_directory():
    """The bug a plain directory rename would leave behind.

    db_path and cache_dir are stored absolute, so an install moved without this
    step loads a catalogue path under a directory that no longer exists.
    """
    legacy = legacy_app_data_dir()
    _populate(legacy)

    migrate_legacy_app_data()

    target = app_data_dir()
    config = _config(target)
    assert config["db_path"] == str(target / "archive.db")
    assert config["cache_dir"] == str(target / "cache")
    assert str(legacy) not in json.dumps(config)


def test_paths_outside_the_old_directory_are_left_alone(tmp_path):
    """Someone who put their database on another volume chose that on purpose."""
    elsewhere = tmp_path / "big-disk" / "trove.db"
    elsewhere.parent.mkdir(parents=True)
    legacy = legacy_app_data_dir()
    _populate(legacy, db_path=elsewhere)

    migrate_legacy_app_data()

    assert _config(app_data_dir())["db_path"] == str(elsewhere)


def test_the_users_own_archive_folders_are_never_rewritten():
    legacy = legacy_app_data_dir()
    _populate(legacy)

    migrate_legacy_app_data()

    assert _config(app_data_dir())["roots"] == ["/home/someone/Photos"]


def test_a_log_directory_alone_does_not_block_the_move():
    """logging_setup creates logs/ on the first record, which happens before the
    migration runs. Treating that as an occupied target would strand every
    upgrading install."""
    legacy = legacy_app_data_dir()
    _populate(legacy)
    target = app_data_dir()
    (target / "logs").mkdir(parents=True)
    (target / "logs" / "trove.log").write_text("a line written during startup")

    assert migrate_legacy_app_data() is True

    assert (target / "archive.db").is_file()
    assert (target / "logs" / "trove.log").read_text() == "a line written during startup"


def test_two_populated_directories_are_both_left_untouched(caplog):
    """A pre-rename and a post-rename build were both run. Merging catalogues is
    not something to attempt silently."""
    legacy = legacy_app_data_dir()
    _populate(legacy)
    target = app_data_dir()
    _populate(target)
    (target / "archive.db").write_text("the newer catalogue")

    with caplog.at_level("WARNING"):
        assert migrate_legacy_app_data() is False

    assert (legacy / "archive.db").read_text() == "not really sqlite, but it is a file"
    assert (target / "archive.db").read_text() == "the newer catalogue"
    assert "both" in caplog.text


def test_a_fresh_install_does_nothing():
    assert not legacy_app_data_dir().exists()
    assert migrate_legacy_app_data() is False
    assert not app_data_dir().exists()


def test_an_empty_legacy_directory_is_not_treated_as_data():
    """An empty shell is not a catalogue worth moving, and moving it would
    create a target that then blocks a real migration later."""
    legacy_app_data_dir().mkdir(parents=True)

    assert migrate_legacy_app_data() is False


def test_running_it_twice_is_a_no_op():
    """It runs on every startup, from either entry point."""
    _populate(legacy_app_data_dir())

    assert migrate_legacy_app_data() is True
    assert migrate_legacy_app_data() is False

    assert (app_data_dir() / "archive.db").is_file()


def test_a_config_that_is_not_readable_json_does_not_stop_startup(caplog):
    """The move already succeeded by then; refusing to start would be worse than
    starting with paths that need fixing by hand."""
    legacy = legacy_app_data_dir()
    _populate(legacy)
    (legacy / "config.json").write_text("{ this is not json")

    with caplog.at_level("WARNING"):
        assert migrate_legacy_app_data() is True

    assert (app_data_dir() / "archive.db").is_file()


@pytest.mark.parametrize("marker", ["config.json", "archive.db", "archives", "cache"])
def test_any_single_artefact_is_enough_to_count_as_an_install(marker):
    """A catalogue interrupted mid-setup still has to come across."""
    legacy = legacy_app_data_dir()
    legacy.mkdir(parents=True)
    if marker in ("archives", "cache"):
        (legacy / marker).mkdir()
    else:
        (legacy / marker).write_text("{}" if marker.endswith(".json") else "data")

    assert migrate_legacy_app_data() is True
    assert (app_data_dir() / marker).exists()
