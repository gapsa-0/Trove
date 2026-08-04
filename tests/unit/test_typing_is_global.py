"""The typing settings are the package's floor, not a list of opted-in packages.

`trove` spent a while with two mypy override blocks moving in
opposite directions -- a "not reviewed yet" list under `ignore_errors` that
only shrank, and a strict list that only grew (ADR 0010). Both are gone: the
strict settings are `[tool.mypy]` globals now, so a package is checked because
it exists rather than because someone remembered to name it.

That is a stronger guarantee than the list ever gave, and it is only stronger
while it stays true. The failure mode it replaces is silent: add a package,
forget the line, and it is unchecked with a green build. The failure mode it
introduces is also silent -- add an override to quiet a package that will not
pass, and it is unchecked with a green build. This test is what makes either
one loud.

It reads `pyproject.toml` rather than running mypy: the question is what the
configuration *says*, and a config assertion that costs 2 ms belongs in the
unit tier next to the layering rule it mirrors.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import trove

REPO_ROOT = Path(trove.__file__).parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"

# The settings that were the strict block's, now the package floor. Losing any
# of them silently downgrades every module at once.
REQUIRED_GLOBALS = {
    "check_untyped_defs": True,
    "disallow_untyped_defs": True,
    "disallow_incomplete_defs": True,
    "strict_optional": True,
    "warn_return_any": True,
    "warn_unused_ignores": True,
}


def _mypy_config() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["mypy"]


def test_the_strict_settings_are_global():
    cfg = _mypy_config()
    for key, expected in REQUIRED_GLOBALS.items():
        assert cfg.get(key) == expected, (
            f"[tool.mypy] {key} must be {expected}: it is the package's floor, "
            "not a per-package opt-in"
        )


def test_no_override_relaxes_a_package():
    """An override may tighten or retarget; it may not switch checking off.

    ``ignore_errors`` and the three ``disallow_*``/``check_*`` switches are the
    ones that turn a module back into unchecked text. A future override for
    something else -- a per-module ``ignore_missing_imports``, say -- is fine
    and deliberately not caught here.
    """
    relaxing = {
        "ignore_errors": True,
        "check_untyped_defs": False,
        "disallow_untyped_defs": False,
        "disallow_incomplete_defs": False,
        "strict_optional": False,
        "warn_return_any": False,
    }
    for override in _mypy_config().get("overrides", []):
        modules = override.get("module")
        for key, off in relaxing.items():
            assert override.get(key) != off, (
                f"override for {modules!r} sets {key}={off!r}, which un-checks it. "
                "The two-list era is over (ADR 0010) -- fix the package instead."
            )


def test_every_type_ignore_names_its_error_code():
    """A bare ``# type: ignore`` silences everything on the line, for ever.

    With a code it silences one named thing, and ``warn_unused_ignores`` deletes
    it for you once it stops being needed. Bare ones are invisible debt; coded
    ones are a claim the checker keeps honest.
    """
    bare = []
    for path in sorted((REPO_ROOT / "trove").rglob("*.py")):
        if "__pycache__" in path.parts or "vendor" in path.parts:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "type: ignore" in line and "type: ignore[" not in line:
                bare.append(f"{path.relative_to(REPO_ROOT)}:{n}")
    assert not bare, "bare `# type: ignore` (use `# type: ignore[code]`): " + ", ".join(bare)
