"""Preprocessing parity against the checkpoint's own reference implementation.

This is the load-bearing test of the SigLIP backend. A wrong resize mode, a
wrong normalisation, or a tokenizer configured differently from training costs
retrieval quality *silently* -- every vector still has the right shape and every
search still returns something, just worse. Nothing else in the app would notice.

So both halves are pinned against the checkpoint's own configuration files --
``preprocessor_config.json`` and ``tokenizer.json``, downloaded next to the ONNX
weights -- rather than against constants copied into the test. Read the values
from the checkpoint and a future model with a different recipe fails the test
instead of silently passing it.

The text half is compared directly against ``transformers``' tokenizer. The image
half is not: transformers 5 routes ``SiglipImageProcessor`` through torchvision,
whose resize is a *different* implementation from the PIL one the checkpoint was
released with, so it is the wrong reference. Instead the recipe is re-implemented
here straight from ``preprocessor_config.json``, independently of
``backend.py``. Neither half needs model weights or torch: the risk being tested
lives in the preprocessing, not in the official ONNX export.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from organize_archive.embeddings import backend as eb
from organize_archive.paths import default_cache_dir

# Resolved at import, deliberately: conftest's isolate_app_data fixture points
# XDG_DATA_HOME at a throwaway directory for every test, so asking for the cache
# dir later would find an empty one and skip even on a machine that has the
# models.
_CACHE = str(default_cache_dir())

transformers = pytest.importorskip(
    "transformers", reason="transformers is a dev-only reference dependency"
)
PIL = pytest.importorskip("PIL")

pytestmark = pytest.mark.skipif(
    not eb.models_ready(_CACHE), reason="SigLIP 2 weights are not downloaded on this machine"
)


def _fixture_images():
    """Deterministic images that would expose a wrong resize.

    Both are deliberately **non-square**: a centre crop or an aspect-preserving
    resize gives a visibly different tensor from SigLIP's squash-to-256, and a
    square fixture could not tell the three apart.
    """
    from PIL import Image

    wide = np.zeros((120, 400, 3), dtype=np.uint8)
    wide[:, :133] = (220, 30, 30)
    wide[:, 133:266] = (30, 220, 30)
    wide[:, 266:] = (30, 30, 220)
    tall = (np.indices((500, 180)).sum(axis=0) % 37 * 7).astype(np.uint8)
    tall = np.repeat(tall[:, :, None], 3, axis=2)
    return [Image.fromarray(wide), Image.fromarray(tall)]


def _preprocessor_config() -> dict:
    return json.loads((eb.models_dir(_CACHE) / "preprocessor_config.json").read_text("utf-8"))


def test_backend_constants_match_the_checkpoints_preprocessor_config():
    """The recipe the backend hardcodes is the one this checkpoint declares.

    ``_pixels`` bakes in 256x256, /255 and (x-0.5)/0.5 for speed. This is the
    test that keeps those constants honest: swap in a checkpoint with a
    different size or normalisation and it fails here, loudly, instead of
    quietly embedding everything wrong.
    """
    cfg = _preprocessor_config()

    assert cfg["size"] == {"height": eb.IMAGE_SIZE, "width": eb.IMAGE_SIZE}
    assert cfg["do_resize"] and cfg["do_rescale"] and cfg["do_normalize"]
    assert cfg["resample"] == 2  # PIL BILINEAR
    assert abs(cfg["rescale_factor"] - 1 / 255) < 1e-12
    assert cfg["image_mean"] == [0.5, 0.5, 0.5]
    assert cfg["image_std"] == [0.5, 0.5, 0.5]


def test_image_preprocessing_matches_the_declared_recipe():
    """Re-implement the config's recipe from scratch and compare.

    Deliberately written the long way round -- read every value out of
    ``preprocessor_config.json``, apply them in order -- so it shares no code
    with ``_pixels`` and can actually disagree with it.
    """
    from PIL import Image

    cfg = _preprocessor_config()
    resample = {0: Image.NEAREST, 1: Image.LANCZOS, 2: Image.BILINEAR, 3: Image.BICUBIC}[
        cfg["resample"]
    ]
    mean = np.asarray(cfg["image_mean"], dtype=np.float32)
    std = np.asarray(cfg["image_std"], dtype=np.float32)
    images = _fixture_images()

    reference = []
    for image in images:
        resized = image.convert("RGB").resize(
            (cfg["size"]["width"], cfg["size"]["height"]), resample
        )
        x = np.asarray(resized, dtype=np.float32) * cfg["rescale_factor"]
        reference.append(((x - mean) / std).transpose(2, 0, 1))
    reference = np.stack(reference)

    ours = np.stack([eb.SiglipBackend._pixels(image) for image in images])

    assert ours.shape == reference.shape == (2, 3, eb.IMAGE_SIZE, eb.IMAGE_SIZE)
    assert ours.dtype == np.float32
    assert np.abs(ours - reference).max() < 1e-6


def test_image_preprocessing_squashes_rather_than_crops():
    """Guards the specific mistake ``thumbnail()``-style code would make.

    Aspect-preserving resize is the intuitive thing to write and the wrong thing
    here, so assert the difference directly rather than trusting the bound above
    to catch it.
    """
    from PIL import Image

    wide = _fixture_images()[0]
    ours = eb.SiglipBackend._pixels(wide)
    aspect_preserved = wide.copy()
    aspect_preserved.thumbnail((256, 256), Image.BILINEAR)
    padded = Image.new("RGB", (256, 256))
    padded.paste(aspect_preserved)

    assert np.abs(ours - eb.SiglipBackend._pixels(padded)).max() > 0.1


def test_pixel_values_are_in_the_trained_range():
    for image in _fixture_images():
        x = eb.SiglipBackend._pixels(image)
        assert x.shape == (3, 256, 256)
        assert -1.0 <= float(x.min()) and float(x.max()) <= 1.0
    # A mid-grey image maps to exactly 0 under (x/255 - 0.5) / 0.5.
    from PIL import Image

    grey = eb.SiglipBackend._pixels(Image.new("RGB", (300, 200), (128, 128, 128)))
    assert abs(float(grey.mean()) - (128 / 255 - 0.5) / 0.5) < 1e-6


QUERIES = [
    "a photo of a dog.",
    "A PHOTO OF A DOG.",
    "cumpleaños en la playa",
    "Cumpleaños en la Playa",
    "niños jugando en el jardín con un perro",
    "wedding party at night",
    "una palabra " * 40,  # long enough to force truncation
]


def _tokenizer():
    backend = eb.SiglipBackend(_CACHE)
    backend.load_text()
    return backend


def test_tokenizer_matches_transformers_on_lowercased_text():
    from transformers import AutoTokenizer

    reference = AutoTokenizer.from_pretrained(str(eb.models_dir(_CACHE)))
    backend = _tokenizer()

    ours = backend._tokenize(QUERIES)
    expected = reference(
        [q.lower() for q in QUERIES],
        padding="max_length",
        max_length=eb.MAX_TOKENS,
        truncation=True,
        return_tensors="np",
    )["input_ids"]

    assert ours.dtype == np.int64
    assert np.array_equal(ours, np.asarray(expected, dtype=np.int64))


def test_tokenizer_pads_to_exactly_64_with_the_checkpoints_pad_id():
    """64 fixed tokens, padded with id 0.

    The model was trained with fixed 64-token padding, so a variable-length
    batch silently degrades results. ``tokenizer.json`` already configures
    ``Fixed(64)`` with ``pad_id`` 0 -- this pins that we inherit it instead of
    overriding it with a guessed pad token, which would push a real token into
    every trailing position.
    """
    backend = _tokenizer()
    ids = backend._tokenize(["lago", "una foto de la familia en la playa"])

    assert ids.shape == (2, eb.MAX_TOKENS)
    assert ids[0][-1] == 0 and ids[1][-1] == 0
    # 1 is <eos>, appended after the real tokens and before the padding.
    assert 1 in ids[0].tolist()


def test_tokenizer_truncates_a_long_query():
    backend = _tokenizer()
    ids = backend._tokenize(["una palabra " * 200])
    assert ids.shape == (1, eb.MAX_TOKENS)


def test_tokenizer_lowercases_because_neither_normaliser_nor_transformers_does():
    """SigLIP 2 was trained on lowercased text.

    ``tokenizer_config.json`` records ``do_lower_case: true``, but the fast
    tokenizer's normaliser only maps spaces to U+2581 -- it does *not* lowercase,
    and neither does transformers. Case therefore has to be folded by us, and
    cased input really does tokenise differently.
    """
    from tokenizers import Tokenizer

    raw = Tokenizer.from_file(str(eb.models_dir(_CACHE) / eb.TOKENIZER))
    assert raw.encode("A PHOTO OF A DOG.").ids != raw.encode("a photo of a dog.").ids

    backend = _tokenizer()
    ids = backend._tokenize(["A PHOTO OF A DOG.", "a photo of a dog."])
    assert np.array_equal(ids[0], ids[1])
