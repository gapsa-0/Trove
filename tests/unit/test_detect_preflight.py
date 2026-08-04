"""What the detect stage checks before it spends a user's bandwidth.

Loading the detectors fetches ~310 MB (buffalo_l for faces, YOLOX for pets) and
then opens two more weights that have no upstream URL — the AdaFace embedder and
the DINOv2 pet model. The old order downloaded first and discovered the
unobtainable weight second, so a source checkout paid for both packs and still
failed; the failure then said "see the messages above", which on a status card is
advice about somewhere the user cannot look.

So: both detectors are preflighted (network-free) before either is constructed,
one detector's failure still leaves the other running, and every reason travels
with the error that stops the stage.
"""

from __future__ import annotations

import pytest

from trove import model_manifest as mm
from trove.config import Config
from trove.detect import extract as dx
from trove.errors import ModelUnavailableError
from trove.faces import backend as face_backend
from trove.pets import backend as pet_backend


@pytest.fixture
def cfg(tmp_path):
    return Config(cache_dir=str(tmp_path / "cache"))


@pytest.fixture
def no_downloads(monkeypatch):
    """Any weight fetch during these tests is the bug they exist to catch."""
    monkeypatch.setattr(face_backend, "available", lambda: True)
    monkeypatch.setattr(pet_backend, "available", lambda: True)
    monkeypatch.setattr(face_backend, "ensure_models", _forbidden)
    monkeypatch.setattr(pet_backend, "ensure_model", _forbidden)
    monkeypatch.setattr(mm, "ensure", _forbidden)


def test_nothing_is_downloaded_when_a_weight_cannot_be_obtained(cfg, monkeypatch, no_downloads):
    monkeypatch.setattr(mm, "obtainable", lambda _name, _cache: False)
    monkeypatch.setattr(face_backend, "FaceBackend", _forbidden)
    monkeypatch.setattr(pet_backend, "PetBackend", _forbidden)
    messages: list[str] = []

    loaded = dx.make_backends(cfg, log=messages.append)

    assert (loaded.face, loaded.pet) == (None, None)
    assert len(loaded.problems) == 2
    assert messages == list(loaded.problems)


def test_the_error_names_both_causes_and_the_tools_that_fix_them(cfg, monkeypatch, no_downloads):
    """The card shows this text, so it has to be the whole answer."""
    monkeypatch.setattr(mm, "obtainable", lambda _name, _cache: False)
    monkeypatch.setattr(face_backend, "FaceBackend", _forbidden)
    monkeypatch.setattr(pet_backend, "PetBackend", _forbidden)

    loaded = dx.make_backends(cfg)

    with pytest.raises(ModelUnavailableError) as caught:
        loaded.require()
    message = str(caught.value)
    assert "people detection unavailable" in message
    assert "pet detection unavailable" in message
    assert "tools/build/adaface_export.py" in message
    assert "tools/build/dinov2_pet_export.py" in message


def test_one_unobtainable_weight_does_not_stop_the_other_detector(cfg, monkeypatch, no_downloads):
    """Faces-only and pets-only are supported states, not failures."""
    monkeypatch.setattr(mm, "obtainable", lambda name, _cache: name != "adaface")
    monkeypatch.setattr(face_backend, "FaceBackend", _forbidden)
    monkeypatch.setattr(pet_backend, "PetBackend", lambda *a, **k: "pet backend")

    loaded = dx.make_backends(cfg)

    assert loaded.face is None
    assert loaded.pet == "pet backend"
    assert len(loaded.problems) == 1
    assert "people detection unavailable" in loaded.problems[0]
    loaded.require()  # one detector is enough to run


def test_a_backend_that_fails_to_load_is_reported_not_raised(cfg, monkeypatch, no_downloads):
    """A preflight cannot promise a load will work; the load still degrades."""
    monkeypatch.setattr(mm, "obtainable", lambda _name, _cache: True)
    monkeypatch.setattr(face_backend, "FaceBackend", _raises("onnxruntime said no"))
    monkeypatch.setattr(pet_backend, "PetBackend", lambda *a, **k: "pet backend")

    loaded = dx.make_backends(cfg)

    assert loaded.face is None
    assert "onnxruntime said no" in loaded.problems[0]


def _forbidden(*_args, **_kwargs):
    raise AssertionError("fetched or built a detector that preflight should have refused")


def _raises(message: str):
    def build(*_args, **_kwargs):
        raise ModelUnavailableError(message)

    return build
