#!/usr/bin/env python3
"""Fail if a tracked file or a function grows past a size budget.

Nothing else in the toolchain notices a module or a function quietly turning
into a 2000-line pile. Ruff and mypy check correctness and style, not shape;
code review catches it only if the reviewer happens to scroll far enough.
Left alone, a "just one more case" habit turns one file into the place every
future change lands, because it is already where everything else landed.

This is a *ratchet*, not a fixed limit: today's known-oversized files and
functions are named explicitly in an allowlist below, at their exact current
size, so the check starts green. From here it can only tighten -- see the
comment above ALLOWLIST for the rule that makes that true.

Usage:  python3 tools/dev/check_sizes.py [--list]
Exits 0 when everything is within budget, 1 otherwise.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

MAX_PY_MODULE, MAX_PY_FUNC = 600, 80
MAX_ASSET = 800  # css / js / html under web/static and web/index.html

# ---------------------------------------------------------------------------
# An entry here may only ever SHRINK. Raising an existing limit, or adding a
# new one, means a file or function grew past budget -- that requires saying
# why in the commit body, same as any other regression. The point of a
# ratchet is that the decision to let something get bigger shows up in a diff
# someone reviews, instead of happening silently one "just one more case" at
# a time. Do not bump a number here to make a failing check pass; shrink the
# code, or get the growth reviewed and justified on purpose.
#
# Keys are repo-relative paths for modules/assets, or "path::function" for
# a single function/method (nested functions and methods are addressed by
# their own name, not a dotted path -- ast.walk does not track nesting).
# ---------------------------------------------------------------------------
ALLOWLIST: dict[str, int] = {
    # Modules
    "organize_archive/faces/cluster.py": 688,
    "tests/gui/test_api_routes.py": 1159,
    "tests/integration/test_merge_undo.py": 838,
    # Functions
    "organize_archive/cli/faces.py::run": 153,
    "organize_archive/cli/scan.py::run": 85,
    "organize_archive/db/database.py::init_db": 98,
    "organize_archive/dedup/exact.py::run": 138,
    "organize_archive/faces/cluster.py::build": 89,
    "organize_archive/faces/cluster.py::cluster_faces": 142,
    "organize_archive/faces/extract.py::extract": 134,
    "organize_archive/faces/migrate_adaface.py::reattach": 91,
    "organize_archive/faces/migrate_adaface.py::snapshot_and_wipe": 95,
    "organize_archive/geo/clusters.py::assign_unplaced": 83,
    "organize_archive/metadata/enrich.py::enrich": 160,
    "organize_archive/pets/backend.py::_forward": 83,
    "organize_archive/pets/cluster.py::cluster_pets": 110,
    "organize_archive/pipeline/scheduler.py::tick": 95,
    "organize_archive/pipeline/stages.py::cards": 104,
    "organize_archive/scan/walker.py::scan_root": 139,
    "tests/gui/test_api_routes.py::_seed_archive": 111,
    "tools/dev/faces_cluster_montage.py::main": 133,
    "tools/dev/faces_umap_plot.py::main": 146,
}


def tracked_files() -> list[str]:
    """Every file `git` is tracking, repo-relative, as `git ls-files` sees it."""
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True)
    return out.stdout.splitlines()


def py_modules(paths: list[str]) -> list[str]:
    """Tracked .py files, excluding tools/build/ (vendored upstream code that
    every tool in this repo treats as out of scope -- it is not ours to keep
    small)."""
    return [p for p in paths if p.endswith(".py") and not p.startswith("tools/build/")]


def assets(paths: list[str]) -> list[str]:
    """Tracked css/js/html under organize_archive/web/, excluding vendor/
    (minified third-party libraries -- also deliberately out of scope)."""
    return [
        p
        for p in paths
        if p.startswith("organize_archive/web/")
        and p.endswith((".css", ".js", ".html"))
        and "/vendor/" not in p
    ]


def line_count(path: Path) -> int:
    """Same count as `wc -l`: the number of newline characters."""
    with path.open("rb") as f:
        return sum(chunk.count(b"\n") for chunk in iter(lambda: f.read(1 << 16), b""))


def func_sizes(path: Path) -> dict[str, int]:
    """Every function/method in a module, by name, sized end_lineno - lineno + 1.

    ast.walk finds nested defs and methods alike, so a giant function hiding
    inside a class or closure is still caught. One name can occur several
    times in a file -- `__init__` on three classes, a `wrapper` inside two
    decorators, a `run` stubbed differently in three tests -- so the largest
    of them wins. Keeping the last one instead would let a two-line stub
    later in the file hide a 200-line namesake above it, which is the one way
    this check could silently stop checking something.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    sizes: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            size = node.end_lineno - node.lineno + 1
            sizes[node.name] = max(size, sizes.get(node.name, 0))
    return sizes


def main() -> int:
    paths = tracked_files()
    modules = py_modules(paths)
    static_assets = assets(paths)

    # subject -> (size, budget, allowlist_key or None)
    subjects: dict[str, tuple[int, int, str | None]] = {}

    for rel in modules:
        p = ROOT / rel
        subjects[rel] = (line_count(p), MAX_PY_MODULE, rel if rel in ALLOWLIST else None)
        for func, size in func_sizes(p).items():
            key = f"{rel}::{func}"
            subjects[key] = (size, MAX_PY_FUNC, key if key in ALLOWLIST else None)

    for rel in static_assets:
        p = ROOT / rel
        subjects[rel] = (line_count(p), MAX_ASSET, rel if rel in ALLOWLIST else None)

    if "--list" in sys.argv:
        for key, (size, budget, _allowed) in sorted(subjects.items()):
            if size > budget:
                print(f"{size}\t{key}")
        return 0

    failures: list[str] = []

    for key, (size, budget, allowed_key) in sorted(subjects.items()):
        limit = ALLOWLIST.get(allowed_key) if allowed_key else None
        if limit is not None:
            if size > limit:
                failures.append(f"{key}: {size} lines, over its allowlist entry of {limit}")
        elif size > budget:
            failures.append(
                f"{key}: {size} lines, over the budget of {budget} (no allowlist entry)"
            )

    # STALE ENTRY: an allowlist entry whose subject no longer exists, or whose
    # subject has shrunk to at or under the global budget -- the entry is no
    # longer earning its keep and must be deleted so the ratchet keeps
    # tightening. This deliberately does NOT fire just because a subject
    # shrank below its entry but is still over budget: demanding the
    # allowlist number track every edit exactly would make this check
    # tedious enough to get disabled, and "still enforced, just at a looser
    # number" is a fine state to leave to the next edit that touches the file.
    for key, limit in sorted(ALLOWLIST.items()):
        if key not in subjects:
            failures.append(f"STALE ENTRY: {key} is allowlisted at {limit} but no longer exists")
            continue
        size, budget, _allowed = subjects[key]
        if size <= budget:
            failures.append(
                f"STALE ENTRY: {key} is allowlisted at {limit} but is now {size} lines, "
                f"at or under the budget of {budget} -- delete the entry"
            )

    if failures:
        print("Size ratchet failures:")
        for f in failures:
            print(f"  {f}")
        print(
            f"\n{len(failures)} failure(s). An allowlist entry may only shrink; "
            "raising one or adding one needs a reason in the commit body."
        )
        return 1

    print(f"OK: {len(subjects)} files and functions checked, all within budget")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
