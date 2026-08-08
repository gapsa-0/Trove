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


def _appended_destinations() -> list[str]:
    """Every destination the spec appends to ``datas``, sorted, read from source.

    The spec is not importable (see the module docstring), so the appends are
    read as AST rather than executed. Sorted because ``ast.walk`` yields by
    breadth rather than by line, and what these tests are about is *which*
    destinations exist, never the order they are written in.
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
    return sorted(destinations)


def test_the_spec_appends_only_the_tools_tree_and_the_manifest():
    """The stronger form: enumerate what actually gets appended to ``datas``.

    A future weights directory under some other name would slip past the string
    check above, so read the appends themselves. Two are expected and neither is
    a weight: the native tools tree, and the manifest that *describes* the
    weights (6 KB of JSON, and the file the next test pins the location of).
    """
    assert _appended_destinations() == ["packaging/models", "tools"], (
        "packaging/trove.spec should append exactly two datas entries, the native "
        f"tools tree and the model manifest. It appends: {_appended_destinations()}. "
        "Anything else is new payload in every installer -- confirm that is intended "
        "before changing this test."
    )


def test_the_spec_puts_the_manifest_where_the_app_looks_for_it():
    """The manifest's destination in the bundle is not a free choice.

    ``model_manifest`` resolves it relative to the package's own parent, which is
    the checkout root in a source tree and the bundle root in a frozen build --
    one expression that has to answer in both, so the spec's destination must be
    the manifest's own path relative to that root.

    Shipped without it, every weight lookup raised instead of answering "not
    here", and since the scheduler asks that on its first step, no build ever ran
    a stage or downloaded a model. The two halves are edited in different files,
    which is why this test holds them together rather than trusting a comment.
    """
    from trove import model_manifest

    expected = model_manifest.MANIFEST_PATH.parent.relative_to(model_manifest.PROJECT_ROOT)
    assert str(expected) in _appended_destinations(), (
        f"model_manifest reads its manifest from {expected}/ inside the bundle, but "
        f"packaging/trove.spec stages datas into {_appended_destinations()}."
    )


# --- the weights that arrive inside packages rather than through `datas` -----
#
# The three checks above watch the spec's own `datas` list, which is where the
# 349 MB of self-exported weights used to enter. Two later payloads got in by a
# different door entirely: `collect_data_files` on a third-party package, which
# sweeps up whatever that package ships. Nothing about them looks like a model
# in this file, which is exactly why they need naming.


def test_the_rapidocr_collection_drops_its_onnx_weights():
    """RapidOCR ships 31.7 MB of ONNX beside the YAML config the engine needs.

    Collected wholesale that is 26.5 MB of compressed installer -- the largest
    single item the bundle ever held -- for a feature most users never switch
    on. They are manifest entries now (ADR 0019), and trove/text/ocr.py passes
    their paths to the engine explicitly. Removing this filter would put them
    back with no other symptom: the build still succeeds, the feature still
    works, and the installer quietly grows.
    """
    source = _spec_source()
    assert 'collect_data_files("rapidocr")' in source, "the spec no longer collects rapidocr at all"
    for line in source.splitlines():
        if 'collect_data_files("rapidocr")' in line and not line.lstrip().startswith("#"):
            assert ".onnx" in line, (
                "packaging/trove.spec collects rapidocr's data files without filtering "
                f"out its ONNX weights:\n  {line.strip()}\n"
                "That is 26.5 MB of compressed installer; see docs/release.md."
            )


def test_the_translator_model_is_not_collected_with_the_app():
    """The Bergamot es-en model and its WASM runtime are ~27 MB under
    trove/web/vendor/, and `collect_data_files("trove")` would take them.

    They belong to Search by description, which already downloads 689 MB of
    SigLIP towers -- 26 more there are invisible, where the same bytes in the
    installer are paid by everyone. trove/translation.py names them; the
    /vendor route serves them from the cache.
    """
    source = _spec_source()
    assert "_FETCHED_VENDOR" in source, (
        "packaging/trove.spec no longer filters the translator's files out of "
        "collect_data_files('trove'). That is 15.9 MB of compressed installer "
        "for a feature that is off by default; see docs/release.md."
    )
    # The filter is only as good as the names in it, so check it still covers
    # every manifest entry the /vendor route expects to serve.
    from trove import translation

    for filename in translation.MODELS.values():
        assert any(
            part in filename for part in ("translate-es-en-", "bergamot-translator-worker")
        ), (
            f"{filename} is fetched at runtime but the spec's _FETCHED_VENDOR "
            "prefixes would not exclude it from the bundle."
        )
