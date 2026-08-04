"""The exiftool binary is resolved once, at construction.

``ExifReader.__init__`` refuses to build unless exiftool is on PATH, and
``read_batch`` then used to look it up *again* on every batch. Between those
two moments PATH can change: a reader validated against one binary could run
against a different one, or against none at all in a long-lived process. The
second lookup was also what forced a `cast(str, ...)` into the command line,
so the type was hiding the window rather than describing it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from trove.metadata import exiftool_reader as ex


@pytest.fixture
def fake_exiftool(monkeypatch, tmp_path):
    """A reader built while `tool()` reports exiftool at a known path."""
    binary = str(tmp_path / "exiftool")
    monkeypatch.setattr(ex, "tool", lambda name: binary if name == "exiftool" else None)
    return binary


def _capture_argv(monkeypatch) -> list[list[str]]:
    """Record the command lines read_batch builds, running none of them."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    monkeypatch.setattr(ex.subprocess, "run", fake_run)
    return calls


def test_the_binary_found_at_construction_is_the_one_that_runs(monkeypatch, fake_exiftool):
    reader = ex.ExifReader()
    calls = _capture_argv(monkeypatch)
    # PATH changes underneath: the lookup would now find a different binary.
    monkeypatch.setattr(ex, "tool", lambda name: "/somewhere/else/exiftool")

    reader.read_batch([Path("/photos/1.jpg")])

    assert calls[0][0] == fake_exiftool


def test_a_reader_keeps_working_when_exiftool_leaves_path(monkeypatch, fake_exiftool):
    """The old code would have put `None` at argv[0] here -- the cast said it
    could not happen, and nothing checked."""
    reader = ex.ExifReader()
    calls = _capture_argv(monkeypatch)
    monkeypatch.setattr(ex, "tool", lambda name: None)

    reader.read_batch([Path("/photos/1.jpg")])

    assert calls[0][0] == fake_exiftool


def test_construction_still_refuses_when_exiftool_is_absent(monkeypatch):
    monkeypatch.setattr(ex, "tool", lambda name: None)

    with pytest.raises(RuntimeError, match="exiftool not found"):
        ex.ExifReader()


def test_an_empty_batch_runs_nothing(monkeypatch, fake_exiftool):
    reader = ex.ExifReader()
    calls = _capture_argv(monkeypatch)

    assert reader.read_batch([]) == {}
    assert calls == []
