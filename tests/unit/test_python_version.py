"""The Python floor is one number, and everything must agree on it.

ADR 0007 says the floor is 3.13 and that it is "pinned consistently across the
toolchain rather than left to drift". Nothing checked that, and it drifted: the
development venv sat on 3.12.3 for a while, so mypy type-checked against a 3.13
standard library that the interpreter running the tests did not have. That is
the quiet kind of wrong -- a stdlib addition mypy accepts and the runtime does
not, invisible until it reaches a machine that runs the code rather than
checking it.

Two separate claims are worth guarding, and this file does both:

* the interpreter actually running the suite satisfies ``requires-python``, so
  a stale venv fails loudly instead of type-checking against the wrong stdlib;
* every other place the version is written down still says the same thing.

Deliberately parsed from the files rather than hardcoded here: a test that
repeats the version is one more copy to drift, and raising the floor on purpose
should mean editing the pins, not this test.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

import organize_archive

ROOT = Path(organize_archive.__file__).resolve().parent.parent
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _floor() -> tuple[int, int]:
    """The ``>=X.Y`` floor from ``requires-python``, as a comparable tuple."""
    spec = PYPROJECT["project"]["requires-python"]
    m = re.fullmatch(r">=\s*(\d+)\.(\d+)", spec.strip())
    assert m, f"requires-python is {spec!r}; this test only understands '>=X.Y'"
    return int(m.group(1)), int(m.group(2))


def test_the_running_interpreter_meets_the_declared_floor():
    """A venv built on an older Python must fail here, not in production.

    This is the check that would have caught the 3.12 venv: everything else in
    ``make check`` was green because ruff and mypy read the *configured* target
    and never ask what is actually executing.
    """
    major, minor = _floor()
    assert sys.version_info[:2] >= (major, minor), (
        f"This interpreter is {sys.version_info.major}.{sys.version_info.minor}, "
        f"but pyproject requires >={major}.{minor}. mypy and ruff are configured "
        f"for {major}.{minor}, so they are checking against a standard library "
        f"this venv does not have.\n"
        f"Rebuild it:  rm -rf .venv && make setup PYTHON=python{major}.{minor}"
    )


def test_mypy_and_ruff_target_the_floor():
    """The two type/lint targets ADR 0007 promises are kept in step."""
    major, minor = _floor()
    tool = PYPROJECT["tool"]
    assert tool["mypy"]["python_version"] == f"{major}.{minor}"
    assert tool["ruff"]["target-version"] == f"py{major}{minor}"


def test_every_workflow_pins_the_floor():
    """CI installs the floor version, on every workflow that installs Python.

    A workflow left on an older ``setup-python`` would test the project on an
    interpreter the project does not support, which is the same divergence as
    the stale venv with a longer feedback loop.
    """
    major, minor = _floor()
    pins = {}
    for wf in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        found = re.findall(r"python-version:\s*'?\"?(\d+\.\d+)", wf.read_text(encoding="utf-8"))
        if found:
            pins[wf.name] = set(found)

    assert pins, "no workflow pins a python-version; has CI moved?"
    wrong = {name: sorted(v) for name, v in pins.items() if v != {f"{major}.{minor}"}}
    assert not wrong, f"workflows not pinned to {major}.{minor}: {wrong}"
