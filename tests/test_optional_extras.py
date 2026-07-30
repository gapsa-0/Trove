"""The graceful-degradation promise, held to by a test rather than by habit.

Every ML dependency in this project is optional, and the contract is the same
everywhere: a missing extra makes the *feature* unavailable, never the app. The
probe pattern that implements it (``available()`` importing inside a try, and a
module-level ``cv2``/``np`` set to None when they will not import) is easy to
break by narrowing an exception, adding a top-level import, or reaching for a
package before asking whether it is there.

Nothing here needs the extras installed. Each test hides a dependency from an
installation that *does* have it, which is the only way to exercise this on a
developer machine or in CI -- both of which install everything.

The one thing these tests deliberately do not claim: that every code path
degrades. ``queries.semantic_search`` does not (it imports numpy outright);
docs/dev/dependencies.md records that gap.
"""

from __future__ import annotations

import hashlib
import sys

import pytest

from organize_archive.config import Config


def hide(monkeypatch, *names: str) -> None:
    """Make ``import name`` raise ImportError for each name given.

    A ``None`` in sys.modules is the interpreter's own "this is known not to be
    importable" marker, so this reproduces a genuinely absent package rather
    than a mocked one -- and monkeypatch puts the real entry back afterwards.
    Submodules are hidden too: blocking ``PIL`` alone leaves ``from PIL import
    Image`` free to succeed from the already-imported submodule.
    """
    for name in names:
        for loaded in [name, *[m for m in list(sys.modules) if m.startswith(f"{name}.")]]:
            monkeypatch.setitem(sys.modules, loaded, None)


class ExplodingFinder:
    """An import hook that fails with something other than ImportError.

    A missing shared library or a mismatched native build does not raise
    ImportError, and that is the entire reason the probes in this codebase
    catch broadly. Reproducing it needs a real import failure, not a stubbed
    module: binding a fake object into sys.modules makes ``import x`` *succeed*.
    """

    def __init__(self, name: str, exc: BaseException) -> None:
        self.name = name
        self.exc = exc

    def find_spec(self, fullname, path=None, target=None):
        if fullname == self.name:
            raise self.exc
        return None


def explode_on_import(monkeypatch, name: str, exc: BaseException) -> None:
    monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [ExplodingFinder(name, exc), *sys.meta_path])


def test_hiding_a_module_really_does_break_importing_it():
    """The other tests are only meaningful if these two mechanisms work."""
    with pytest.MonkeyPatch.context() as mp:
        hide(mp, "colorsys")
        with pytest.raises(ImportError):
            import colorsys  # noqa: F401

    with pytest.MonkeyPatch.context() as mp:
        explode_on_import(mp, "wave", OSError("libfoo.so: cannot open shared object file"))
        with pytest.raises(OSError):
            import wave  # noqa: F401


def test_the_faces_backend_reports_unavailable_instead_of_raising(monkeypatch):
    from organize_archive.faces import backend

    # Both halves of the probe: the module-level cv2/numpy pair that failed at
    # import time on a machine without them, and the in-function import.
    monkeypatch.setattr(backend, "cv2", None)
    monkeypatch.setattr(backend, "np", None)
    hide(monkeypatch, "insightface", "onnxruntime")
    assert backend.available() is False


def test_the_pets_backend_reports_unavailable_instead_of_raising(monkeypatch):
    from organize_archive.pets import backend

    monkeypatch.setattr(backend, "cv2", None)
    monkeypatch.setattr(backend, "np", None)
    assert backend.available() is False


def test_the_semantic_backend_reports_unavailable_instead_of_raising(monkeypatch):
    from organize_archive.embeddings import backend

    monkeypatch.setattr(backend, "np", None)
    hide(monkeypatch, "onnxruntime", "tokenizers", "PIL")
    assert backend.available() is False


def test_a_half_installed_native_package_degrades_rather_than_crashing(monkeypatch):
    """The reason these handlers are broad, kept honest.

    A missing shared library surfaces as OSError and a mismatched onnxruntime
    build as RuntimeError, neither of which is an ImportError. Narrowing the
    handlers would turn a degraded feature into a crash on a real machine, so
    that is what this asserts -- an exception that is *not* ImportError.
    """
    from organize_archive.faces import backend

    if backend.cv2 is None or backend.np is None:
        pytest.skip("needs OpenCV and numpy present, so the probe reaches its import")

    # cv2/np are left real on purpose: the early `cv2 is None` return would
    # otherwise short-circuit before the try block this test is about.
    explode_on_import(
        monkeypatch, "insightface", OSError("libcuda.so.1: cannot open shared object file")
    )
    assert backend.available() is False


def test_perceptual_dedup_switches_off_but_exact_dedup_is_untouched(monkeypatch, tmp_path):
    """Losing the media extra costs near-duplicate detection, not deduplication."""
    from organize_archive.dedup import exact
    from organize_archive.hashing import hasher

    hide(monkeypatch, "imagehash", "PIL")
    assert exact.perceptual_available() is False

    # Exact dedup is stdlib hashlib and keeps working with nothing installed.
    sample = tmp_path / "photo.jpg"
    sample.write_bytes(b"not really a jpeg, but bytes are bytes")
    assert hasher.sha256(sample) == hashlib.sha256(sample.read_bytes()).hexdigest()


def test_the_pipeline_still_offers_every_stage_that_needs_no_extras(monkeypatch, tmp_path):
    """The core stays available when every optional dependency is gone.

    This is the promise that matters at the app level: scanning, enriching,
    deduplicating and placing an archive are the product's spine, and they run
    on the standard library. Only the two model-backed stages drop out.
    """
    from organize_archive.gui import pipeline

    monkeypatch.setattr("organize_archive.detect.extract.available", lambda: False)
    monkeypatch.setattr("organize_archive.gui.semantic.available", lambda: False)

    avail = pipeline._availability(Config(db_path=str(tmp_path / "archive.db")))

    assert avail[pipeline.DETECT] is False
    assert avail[pipeline.SEMANTIC] is False
    assert all(
        avail[stage] for stage in (pipeline.SCAN, pipeline.ENRICH, pipeline.DEDUP, pipeline.PLACES)
    )


def test_availability_is_decided_without_touching_the_disk(tmp_path):
    """An unavailable stage is never queued, and the stage is what downloads its
    own weights -- so gating availability on the weights being present would be
    a deadlock. The probes must therefore ask only "is it importable"."""
    from organize_archive.embeddings import backend

    if not backend.available():
        pytest.skip("the semantic extra is not installed here")

    # Weights absent, stage still offered. Inverting this is the deadlock.
    assert backend.models_ready(str(tmp_path / "nothing-here")) is False
    assert backend.available() is True


def test_preflight_names_the_system_tools_that_are_missing(monkeypatch):
    """The oldest degradation path in the app, and the model for the rest:
    exiftool and ffprobe are reported by name, not assumed and not fatal."""
    from organize_archive import cli

    monkeypatch.setattr(cli, "runtime_tool", lambda name: None)
    assert cli._preflight() == ["exiftool", "ffprobe"]

    monkeypatch.setattr(cli, "runtime_tool", lambda name: None if name == "ffprobe" else "/usr/bin")
    assert cli._preflight() == ["ffprobe"]
