"""The log file is the whole point of the logging work: if these break, a user
reporting "it froze" has nothing to send."""

from __future__ import annotations

import logging

import pytest

from organize_archive import logging_setup


@pytest.fixture(autouse=True)
def _restore_root_logger():
    """Leave the root logger exactly as it was.

    configure() installs global handlers. Without this, one test's handlers stay
    attached for the rest of the session and pytest's own caplog assertions in
    other modules start seeing duplicated records.
    """
    root = logging.getLogger()
    before, level = root.handlers[:], root.level
    yield
    for handler in root.handlers[:]:
        if handler not in before:
            root.removeHandler(handler)
            handler.close()
    for handler in before:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(level)


def _ours(root: logging.Logger) -> list[logging.Handler]:
    return [h for h in root.handlers if getattr(h, "_organize_archive_handler", False)]


def test_configure_writes_under_the_isolated_data_dir(monkeypatch):
    monkeypatch.delenv("OA_LOG_LEVEL", raising=False)
    logging_setup.configure()

    logging.getLogger("organize_archive.test").info("hello from the test")
    for handler in _ours(logging.getLogger()):
        handler.flush()

    path = logging_setup.log_file()
    # The conftest fixture points XDG_DATA_HOME at a tmp dir; a log written
    # outside it would mean the app can write next to a user's real archive.
    assert path.is_file()
    assert "hello from the test" in path.read_text(encoding="utf-8")


def test_configure_twice_does_not_duplicate_handlers(monkeypatch):
    monkeypatch.delenv("OA_LOG_LEVEL", raising=False)
    logging_setup.configure()
    first = len(_ours(logging.getLogger()))
    logging_setup.configure()

    assert len(_ours(logging.getLogger())) == first

    logging.getLogger("organize_archive.test").info("once only")
    for handler in _ours(logging.getLogger()):
        handler.flush()
    assert logging_setup.log_file().read_text(encoding="utf-8").count("once only") == 1


def test_env_var_sets_the_level(monkeypatch):
    monkeypatch.setenv("OA_LOG_LEVEL", "debug")
    logging_setup.configure()
    assert logging.getLogger().level == logging.DEBUG


def test_argument_overrides_the_env_var(monkeypatch):
    monkeypatch.setenv("OA_LOG_LEVEL", "DEBUG")
    logging_setup.configure(level="WARNING")
    assert logging.getLogger().level == logging.WARNING


def test_unknown_level_falls_back_to_info(monkeypatch):
    # A typo in an env var must not stop the application starting.
    monkeypatch.setenv("OA_LOG_LEVEL", "verbose-please")
    logging_setup.configure()
    assert logging.getLogger().level == logging.INFO


def test_stderr_handler_is_optional(monkeypatch):
    monkeypatch.delenv("OA_LOG_LEVEL", raising=False)
    logging_setup.configure(stderr=False)
    assert not any(
        type(h) is logging.StreamHandler  # RotatingFileHandler subclasses it
        for h in _ours(logging.getLogger())
    )


def test_unwritable_log_dir_still_configures(monkeypatch, tmp_path, capsys):
    # A packaged app on a read-only data dir must degrade to stderr, not die.
    blocker = tmp_path / "data"
    blocker.mkdir()
    (blocker / "logs").write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("XDG_DATA_HOME", str(blocker.parent))
    monkeypatch.setattr(logging_setup, "app_data_dir", lambda: blocker)

    logging_setup.configure()

    assert "could not open log file" in capsys.readouterr().err
    assert _ours(logging.getLogger())  # the stderr handler is still there
