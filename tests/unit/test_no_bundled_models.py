"""The frozen build must not carry model weights.

Bundling the two weights in packaging/models/manifest.json (AdaFace 249 MB,
DINOv2-pet 84 MB) was 349 MB of a 744 MB installer -- for files the app already
knows how to fetch, hash-verified, on first use. They are re-published as release
assets on this repository, so ``trove.model_manifest`` resolves them
through its download tier and the spec bundles nothing.

That is easy to undo by accident: re-adding a ``datas`` entry is one line, the
build still succeeds, and the only symptom is an installer that quietly doubles.
So assert the absence directly.

The spec is read as text rather than executed -- it is not an importable module,
and running it needs PyInstaller's globals (``SPECPATH``, ``Analysis``, ``EXE``),
which are not a test dependency.
"""

from __future__ import annotations

import ast
from pathlib import Path

SPEC = Path(__file__).resolve().parents[2] / "packaging" / "trove.spec"


def _spec_source() -> str:
    return SPEC.read_text(encoding="utf-8")


def test_the_spec_file_is_where_this_test_thinks_it_is():
    """Guard the guard: a missing spec would make every assertion below vacuous."""
    assert SPEC.is_file(), f"packaging spec not found at {SPEC}"
    assert "Analysis(" in _spec_source()


def test_the_spec_stages_no_model_weights():
    source = _spec_source()
    # The staging directory is the only route models could take into the bundle.
    offenders = [
        f"  line {i}: {line.strip()}"
        for i, line in enumerate(source.splitlines(), start=1)
        if "models" in line and "staged" in line and not line.lstrip().startswith("#")
    ]
    assert not offenders, (
        "The spec looks like it bundles staged model weights again. That is 349 MB "
        "of installer for files trove.model_manifest downloads on first "
        "use; see docs/release.md.\n" + "\n".join(offenders)
    )


def test_the_spec_appends_only_the_tools_datas_entry():
    """The stronger form: enumerate what actually gets appended to ``datas``.

    A future weights directory under some other name would slip past the string
    check above, so read the appends themselves.
    """
    tree = ast.parse(_spec_source(), filename=str(SPEC))
    destinations = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "append"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "datas"
            and node.args
            and isinstance(node.args[0], ast.Tuple)
            and len(node.args[0].elts) == 2
            and isinstance(node.args[0].elts[1], ast.Constant)
        ):
            destinations.append(node.args[0].elts[1].value)

    assert destinations == ["tools"], (
        "packaging/trove.spec should append exactly one datas entry, the "
        f"native tools tree. It appends: {destinations}. Anything else is new payload "
        "in every installer -- confirm that is intended before changing this test."
    )
