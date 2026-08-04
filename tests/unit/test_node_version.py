"""The Node version is one number, and everything must agree on it.

ADR 0014 says the desktop toolchain runs on Node 22 and that the version is
pinned rather than left to whatever a developer happens to have. Nothing checked
that, and the failure it allows is unusually quiet: on Node 26, Electron's
postinstall unpacks one file of its binary, hangs, and lets the process exit 0,
so ``npm ci`` reports a clean install and ``electron .`` dies at runtime with
"Electron failed to install correctly". ``make setup`` was green throughout.

``desktop/.npmrc`` sets ``engine-strict``, so the ``engines`` range below is what
actually refuses the wrong Node. That makes these three files load-bearing rather
than documentation, which is the reason to guard them:

* ``.nvmrc`` -- what a version manager selects, and the number CI and the docs are
  written against. It sits at the repository root rather than beside
  ``desktop/package.json`` because ``make setup`` runs ``npm ci`` from the root, and
  version managers search upwards, so one file at the top covers both;
* ``desktop/package.json``'s ``engines.node`` -- what npm enforces;
* every workflow's ``node-version`` -- what CI actually installs.

Deliberately parsed from the files rather than hardcoded here, for the same
reason as ``test_python_version.py``: a test that repeats the version is one more
copy to drift, and moving to a new Node on purpose should mean editing the pins,
not this test.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import trove

ROOT = Path(trove.__file__).resolve().parent.parent
DESKTOP = ROOT / "desktop"


def _pinned_major() -> int:
    """The Node major from the root ``.nvmrc``, the source of truth."""
    text = (ROOT / ".nvmrc").read_text(encoding="utf-8").strip()
    m = re.fullmatch(r"v?(\d+)(?:\.\d+){0,2}", text)
    assert m, f".nvmrc is {text!r}; this test only understands a plain version"
    return int(m.group(1))


def test_npm_enforces_the_pin_rather_than_warning():
    """``engine-strict`` is what turns ``engines`` from advice into a gate.

    Without it npm prints EBADENGINE and installs anyway, which is the exact
    outcome this whole guard exists to prevent.
    """
    npmrc = (DESKTOP / ".npmrc").read_text(encoding="utf-8")
    assert re.search(r"^engine-strict\s*=\s*true$", npmrc, re.MULTILINE), (
        "desktop/.npmrc must set engine-strict=true, or package.json's engines "
        "range only warns and a broken Node installs anyway"
    )


def test_the_engines_range_admits_the_pin_and_excludes_the_broken_node():
    """``engines.node`` must accept .nvmrc's version and reject the known-bad one.

    Only the two bounds are checked, not the whole semver grammar: the point is
    that the declared range and the pinned version cannot disagree, and that
    whatever the range says still keeps Node 26 out.
    """
    package = json.loads((DESKTOP / "package.json").read_text(encoding="utf-8"))
    spec = package.get("engines", {}).get("node")
    assert spec, "desktop/package.json must declare engines.node"
    bounds = dict(re.findall(r"(>=|<)\s*(\d+)", spec))
    assert ">=" in bounds and "<" in bounds, (
        f"engines.node is {spec!r}; this test expects a '>=X <Y' range"
    )
    low, high = int(bounds[">="]), int(bounds["<"])
    major = _pinned_major()
    assert low <= major < high, (
        f".nvmrc pins Node {major}, which engines.node ({spec}) does not allow"
    )
    assert high <= 26, (
        f"engines.node ({spec}) admits Node 26, where Electron's postinstall "
        f"unpacks nothing and still exits 0 -- see ADR 0014"
    )


def test_every_workflow_pins_the_same_node():
    """CI installs the pinned version, on every workflow that installs Node.

    A workflow left on another version would build the desktop app on a Node the
    project does not support, which is the same divergence as a developer's
    machine with a longer feedback loop.
    """
    major = _pinned_major()
    pins = {}
    for wf in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        found = re.findall(r"node-version:\s*'?\"?(\d+)", wf.read_text(encoding="utf-8"))
        if found:
            pins[wf.name] = set(found)

    assert pins, "no workflow pins a node-version; has CI moved?"
    wrong = {name: sorted(v) for name, v in pins.items() if v != {str(major)}}
    assert not wrong, f"workflows not pinned to Node {major}: {wrong}"
