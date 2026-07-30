"""`oa logs` turns "navigate to your application data folder" into one command.

The path differs on every OS, so the command existing at all is the point; these
tests pin that it reports the right file, survives the file not existing yet, and
does not create anything just by being asked where the log is.
"""

from __future__ import annotations

from organize_archive.cli import main
from organize_archive.logging_setup import log_file


def test_path_reports_the_log_file(capsys):
    assert main(["logs", "--path"]) == 0
    assert capsys.readouterr().out.strip() == str(log_file())


def test_path_creates_nothing(tmp_path, monkeypatch, capsys):
    """Asking where the log is must not materialise the data directory.

    The same invariant test_logging_setup.py pins for configure(), checked
    through the command a user actually runs.
    """
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "fresh"))
    assert main(["logs", "--path"]) == 0
    capsys.readouterr()
    assert not (tmp_path / "fresh").exists()


def test_missing_log_file_explains_itself(capsys):
    # Exit 1, and a sentence rather than a traceback or an empty response.
    assert main(["logs"]) == 1
    out = capsys.readouterr().out
    assert "No log file yet" in out
    assert "first time something is logged" in out


def test_tail_prints_the_last_lines(capsys):
    path = log_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"line {i}\n" for i in range(1, 11)), encoding="utf-8")

    assert main(["logs", "--tail", "3"]) == 0
    assert capsys.readouterr().out == "line 8\nline 9\nline 10\n"


def test_tail_zero_prints_the_whole_file(capsys):
    path = log_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("first\nsecond\n", encoding="utf-8")

    assert main(["logs", "--tail", "0"]) == 0
    assert capsys.readouterr().out == "first\nsecond\n"


def test_undecodable_bytes_do_not_crash_the_tail(capsys):
    # A log truncated mid-character by rotation must still be readable.
    path = log_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"good line\n\xff\xfe broken\n")

    assert main(["logs"]) == 0
    assert "good line" in capsys.readouterr().out
