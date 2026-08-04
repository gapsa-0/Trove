"""The package's layering, as an executable rule rather than a convention.

`trove` started with the layout ARCHITECTURE.md describes and grew a
second, undeclared structure inside the package now called `web/` — which is
how a face-clustering algorithm ended up importing the web layer:

    detect/extract.py    from ..gui import thumbs
    faces/cluster.py     from ..gui.queries import repair_manual_person_files

(Those read `gui` because that is what `web/` was called when they existed.
The misleading name is a large part of how four layers ended up in one
folder.) Both imports are gone now. This test is what stops them coming
back, because a rule nobody checks is a rule that decays.

The rule: a module may import from its own layer or any layer BELOW it, never
above. Four layers, lowest first:

    L0 foundation   config paths runtime logging_setup errors db
    L1 domain       scan hashing metadata media dedup geo detect faces pets
                    embeddings thumbnails
    L2 application  services pipeline
    L3 delivery     web cli desktop

Two properties are checked, and the second is the one that is easy to get
wrong: **function-local imports count**. A deferred import written inside a
function body is exactly where a violation hides -- both of the two above were
written that way, specifically to dodge the import cycle they created. A cycle
broken by a deferred import is still a cycle, so this walks the whole AST
rather than reading only the top-level import block.

Third property, and the reason this test cannot rot quietly: every top-level
name under `trove/` must appear in LAYERS. Without that, adding a
new package would silently escape the rule instead of failing here.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

import trove

PKG_ROOT = Path(trove.__file__).parent

# The architecture, in one dict. Keyed by the top-level module or package name
# directly under trove/; the value is its layer.
LAYERS = {
    # L0 foundation -- knows nothing about the rest of the package.
    "config": 0,
    "paths": 0,
    "app_data_migration": 0,
    "runtime": 0,
    "model_manifest": 0,
    "features": 0,
    "progress": 0,
    "logging_setup": 0,
    "errors": 0,
    "db": 0,
    # L1 domain -- knows L0 and its siblings, never the application or delivery.
    "scan": 1,
    "hashing": 1,
    "metadata": 1,
    "media": 1,
    "dedup": 1,
    "geo": 1,
    "detect": 1,
    "faces": 1,
    "pets": 1,
    "embeddings": 1,
    "thumbnails": 1,
    # L2 application -- orchestrates L1. Populated by later work; the layer
    # exists here so the rule is in place before the code arrives.
    "services": 2,
    "pipeline": 2,
    # L3 delivery -- translates input to one service call and serialises out.
    "web": 3,
    "cli": 3,
    "desktop": 3,
    "__main__": 3,  # `python -m trove`, which is just cli.main
}

# Known-bad edges, each with a reason and a date. An entry here is debt, not
# permission. It is deliberately EMPTY: the two violations this package used to
# have were both fixed rather than grandfathered, and it should stay empty.
ALLOWED_VIOLATIONS: dict[tuple[str, str], str] = {}


def _module_name(path: Path) -> str:
    """Dotted name of a file inside the package, relative to the package root."""
    parts = list(path.relative_to(PKG_ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _source_files() -> list[Path]:
    return [
        path
        for path in sorted(PKG_ROOT.rglob("*.py"))
        # vendor/ holds third-party assets, which are excluded from every tool
        # in this repo; __pycache__ is not source.
        if "__pycache__" not in path.parts and "vendor" not in path.parts
    ]


def _imports(path: Path, me: str) -> list[tuple[str, int]]:
    """Every intra-package module this file imports, with its line number.

    Walks the entire tree, so imports nested inside functions, methods and
    `try:` blocks are found too -- that is the whole point.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    # A module's package is its parent. A package's __init__ IS the package, so
    # `from .x import y` there means a sibling of __init__, not an uncle.
    is_init = path.name == "__init__.py"
    pkg = me.split(".") if is_init else me.split(".")[:-1]

    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg[: len(pkg) - (node.level - 1)]
                if node.module:
                    found.append((".".join([*base, node.module]), node.lineno))
                else:
                    # `from .. import thumbnails` -- the imported module is the
                    # alias, not node.module, which is None here. Missing this
                    # case would make the plainest cross-package import in the
                    # package invisible to this test.
                    found += [(".".join([*base, a.name]), node.lineno) for a in node.names]
            elif node.module and node.module.startswith("trove"):
                found.append((node.module[len("trove.") :], node.lineno))
        elif isinstance(node, ast.Import):
            found += [
                (a.name[len("trove.") :], node.lineno)
                for a in node.names
                if a.name.startswith("trove.")
            ]
    return found


def _top(module: str) -> str:
    return module.split(".")[0]


def test_every_top_level_name_has_a_declared_layer():
    """A new package must be placed in the layer map, not silently exempted.

    This is what keeps the two tests below honest: they skip edges whose
    endpoints are unknown, so an unmapped package would be unchecked rather
    than rejected.
    """
    on_disk = set()
    for path in _source_files():
        name = _module_name(path)
        if name:  # the package's own __init__.py yields ""
            on_disk.add(_top(name))

    undeclared = sorted(on_disk - set(LAYERS))
    assert not undeclared, (
        f"these live under trove/ but have no layer: {undeclared}. "
        "Add them to LAYERS in this file, in the layer they belong to."
    )


def test_imports_never_point_upwards():
    violations = []
    for path in _source_files():
        me = _module_name(path)
        if not me:
            continue
        mine = _top(me)
        if mine not in LAYERS:
            continue  # covered by the completeness test above
        for imported, lineno in _imports(path, me):
            theirs = _top(imported)
            if theirs not in LAYERS or theirs == mine:
                continue
            if LAYERS[mine] >= LAYERS[theirs]:
                continue
            if (me, imported) in ALLOWED_VIOLATIONS:
                continue
            violations.append(
                f"{path.relative_to(PKG_ROOT.parent)}:{lineno}: "
                f"{me} (L{LAYERS[mine]}) imports {imported} (L{LAYERS[theirs]})"
            )

    assert not violations, "imports point upwards through the layers:\n  " + "\n  ".join(
        sorted(violations)
    )


def test_the_package_graph_is_acyclic():
    """No two packages may depend on each other, in either direction.

    Checked between top-level packages rather than between modules: a mutual
    dependency at that granularity is the architectural smell, and Python
    already refuses genuine top-level module cycles at import time.
    """
    edges: dict[str, set[str]] = defaultdict(set)
    for path in _source_files():
        me = _module_name(path)
        if not me:
            continue
        mine = _top(me)
        for imported, _lineno in _imports(path, me):
            theirs = _top(imported)
            if theirs in LAYERS and mine in LAYERS and theirs != mine:
                edges[mine].add(theirs)

    cycles = []
    visiting: list[str] = []
    done: set[str] = set()

    def walk(node: str) -> None:
        if node in done:
            return
        if node in visiting:
            cycles.append(" -> ".join([*visiting[visiting.index(node) :], node]))
            return
        visiting.append(node)
        for nxt in sorted(edges.get(node, ())):
            walk(nxt)
        visiting.pop()
        done.add(node)

    for start in sorted(edges):
        walk(start)

    assert not cycles, "package import cycles:\n  " + "\n  ".join(sorted(set(cycles)))
