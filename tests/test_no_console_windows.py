"""Every child process must be spawned without a console window.

The Windows desktop build is a GUI-subsystem exe (``console=`` in
packaging/organize-archive.spec), so it owns no console. Windows then allocates
a *fresh* console window for each console-subsystem child -- exiftool, ffmpeg --
which flashes up and vanishes. A pipeline run does that tens of thousands of
times and the app reads as malware.

The trap is that this is invisible to anyone developing on Linux or macOS,
where a child simply inherits stdio: a new ``subprocess.run`` looks perfectly
fine locally and only misbehaves on a user's machine. So rather than trust
review to catch it, walk the shipped package's AST and require every spawn to
carry the suppression.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from organize_archive.runtime import no_window

PACKAGE = Path(__file__).resolve().parent.parent / "organize_archive"

# Everything in subprocess that starts a process.
_SPAWNERS = {"run", "Popen", "call", "check_call", "check_output"}


def _spawn_calls(tree: ast.AST):
    """Yield every ``subprocess.<spawner>(...)`` Call node in a parsed module."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in _SPAWNERS
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
        ):
            yield node


def _suppresses_console(call: ast.Call) -> bool:
    """True if the call passes ``**no_window()`` or sets creationflags itself."""
    for kw in call.keywords:
        if kw.arg == "creationflags":
            return True
        # kw.arg is None for **kwargs unpacking; accept **no_window().
        if (
            kw.arg is None
            and isinstance(kw.value, ast.Call)
            and isinstance(kw.value.func, ast.Name)
            and kw.value.func.id == "no_window"
        ):
            return True
    return False


def test_every_subprocess_spawn_suppresses_its_console():
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for call in _spawn_calls(tree):
            if not _suppresses_console(call):
                rel = path.relative_to(PACKAGE.parent)
                offenders.append(f"  {rel}:{call.lineno}")

    assert not offenders, (
        "These spawns would flash a console window on the Windows build.\n"
        "Add **no_window() (organize_archive.runtime) to each:\n" + "\n".join(offenders)
    )


def test_no_window_is_empty_off_windows(monkeypatch):
    """On Linux/macOS the helper must add nothing at all to the call."""
    monkeypatch.setattr(sys, "platform", "linux")
    assert no_window() == {}


def test_no_window_sets_create_no_window_on_windows(monkeypatch):
    """The branch that only ever runs on a machine this suite can't run on.

    CREATE_NO_WINDOW does not exist in subprocess off Windows, so it is faked
    here; the point is that the helper asks for it, not what the constant is.
    """
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    assert no_window() == {"creationflags": 0x08000000}


def test_the_scanner_would_actually_catch_a_bare_spawn():
    """Guard the guard: a checker that silently matches nothing is worthless."""
    bare = ast.parse("import subprocess\nsubprocess.run(['ffmpeg'])\n")
    guarded = ast.parse("import subprocess\nsubprocess.run(['ffmpeg'], **no_window())\n")

    assert [not _suppresses_console(c) for c in _spawn_calls(bare)] == [True]
    assert [_suppresses_console(c) for c in _spawn_calls(guarded)] == [True]
