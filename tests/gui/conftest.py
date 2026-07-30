"""Fixtures for the tier that runs a JobManager, the pipeline or a live server."""

import pytest

from organize_archive.config import Config


@pytest.fixture
def contained_config(tmp_path, monkeypatch):
    """A Config whose per-archive database and cache resolve under tmp_path.

    This is not a convenience. `archive_db_path`/`archive_cache_dir` normally
    resolve under the user's real ~/.local/share/organize_archive, and a
    JobManager built on an unredirected Config can start real work against the
    real 500 GB archive -- the autouse XDG_DATA_HOME fixture in the parent
    conftest is the other half of the same guard. Four gui tests hand-rolled
    this identical pair of monkeypatches.
    """
    monkeypatch.setattr(Config, "archive_db_path", lambda self, aid: str(tmp_path / "archive.db"))
    monkeypatch.setattr(Config, "archive_cache_dir", lambda self, aid: str(tmp_path / "cache"))
    return Config()
