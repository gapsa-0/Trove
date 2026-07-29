"""Local multimodal embedder: SigLIP 2 base/16 @256, on onnxruntime CPU.

Mirrors ``faces/backend.py`` and ``pets/backend.py`` deliberately, so a reader
who knows one knows all three: an ``available()`` import probe, a
``models_ready``/``ensure_models`` pair that fetches weights once into
``cache/models/siglip2/``, and a class holding the loaded ONNX sessions.

This replaces the Voyage multimodal API — the one place the app used to send
photos and search queries off the machine. Both towers of
``google/siglip2-base-patch16-256`` run here: the vision tower turns archive
media into 768-d vectors during indexing, the text tower turns a typed query
into a vector in the same space at search time. Cosine similarity between the
two is the ranking signal (``gui/queries.py:semantic_search``).

**Two towers, two lifetimes.** The sessions are created lazily and
independently: an indexing job must never pay the ~1 s load and ~700 MB peak of
the text tower, and a search must never pay for the vision tower. The text
weights are not even downloaded until the first search, so a first run blocks on
372 MB rather than 689 MB.

Apache-2.0 weights, official ONNX exports (``onnx-community/…-ONNX``), so unlike
AdaFace and DINOv2 there is no ``tools/build/*_export.py`` to write or maintain.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import threading
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    import numpy as np
except Exception:  # pragma: no cover - optional dep
    # Broad on purpose: a half-installed numpy can fail in more ways than
    # ImportError (e.g. a missing shared library surfaces as OSError).
    # Running without this optional dependency is supported, so DEBUG only.
    logger.debug("numpy import failed; embeddings backend will report unavailable", exc_info=True)
    np = None

# Identity of the vector space `semantic_embeddings` holds. Recorded per row via
# gui/semantic.py's INDEXER_VERSION; when it stops matching, every stored vector
# was produced by a different model and is not comparable to a fresh query, so
# the archive re-indexes itself. BUMP THIS whenever the model, the tower, or the
# preprocessing below changes.
EMBEDDER_VERSION = "siglip2-b16-256-v1"

DIMENSIONS = 768
IMAGE_SIZE = 256
MAX_TOKENS = 64

MODELS_SUBDIR = "siglip2"
VISION_MODEL = "vision_model.onnx"
TEXT_MODEL = "text_model_int8.onnx"
TOKENIZER = "tokenizer.json"

# Pinned revision of onnx-community/siglip2-base-patch16-256-ONNX. Pinned rather
# than "main" so a repo update can never silently swap the vector space under an
# already-indexed archive.
_HF_REVISION = "d1114256522a37ffa257a0a58017348ab0058db2"
_HF_BASE = (
    f"https://huggingface.co/onnx-community/siglip2-base-patch16-256-ONNX/resolve/{_HF_REVISION}/"
)
# Deliberately the only origin. A release-asset mirror was written here first,
# for the CI-reproducibility reason packaging/models/manifest.json gives, but the
# assets were never uploaded — so the fallback only ever turned a clear "Hugging
# Face is unreachable" into a confusing second 404. If a mirror is wanted later,
# upload the three files under exactly the names above *before* pointing at them.

# name on disk -> (path within the HF repo, exact size, sha256)
_FILES: dict[str, tuple[str, int, str]] = {
    VISION_MODEL: (
        "onnx/vision_model.onnx",
        371_992_072,
        "f5cb16728a704703f05516ded628397e11dbca4de2eb5db04b0c0bcee988aa7a",
    ),
    TEXT_MODEL: (
        "onnx/text_model_int8.onnx",
        283_438_275,
        "6f59b39d880c413042314b79302b74d0dd93b273caf8fbfdb1eb2df61a7fefd4",
    ),
    TOKENIZER: (
        "tokenizer.json",
        34_363_039,
        "cb9140fae3ac5122c972d37adf83e1248471a38147ad76f8215c8872c6fd8322",
    ),
}

# What each half of the feature needs. Indexing wants only the vision tower;
# search wants the text tower and the tokenizer.
_VISION_FILES = (VISION_MODEL,)
_TEXT_FILES = (TEXT_MODEL, TOKENIZER)


def available() -> bool:
    """True if onnxruntime, tokenizers, numpy and Pillow are importable.

    Deliberately does *not* check for downloaded weights: this is the predicate
    that decides whether the pipeline offers the stage at all, and the stage is
    what downloads them (see ensure_models). Gating availability on the files
    would deadlock — the stage would never run, so they would never arrive.
    """
    if np is None:
        return False
    try:
        # Importing is the probe; the names are unused on purpose.
        import onnxruntime  # noqa: F401
        import tokenizers  # noqa: F401
        from PIL import Image  # noqa: F401

        return True
    except Exception:  # pragma: no cover - optional dep
        # Broad on purpose: onnxruntime/tokenizers/Pillow can fail in more ways
        # than ImportError (a mismatched onnxruntime build -> RuntimeError, a
        # missing shared library -> OSError). Running without semantic search is
        # a supported configuration, so DEBUG only.
        logger.debug(
            "onnxruntime/tokenizers/Pillow unavailable; semantic search disabled",
            exc_info=True,
        )
        return False


def models_dir(cache_dir: str) -> Path:
    return Path(cache_dir) / "models" / MODELS_SUBDIR


def _present(cache_dir: str, names) -> bool:
    d = models_dir(cache_dir)
    return all((d / n).is_file() and (d / n).stat().st_size == _FILES[n][1] for n in names)


def vision_ready(cache_dir: str) -> bool:
    """The indexing half: just the vision tower."""
    return _present(cache_dir, _VISION_FILES)


def text_ready(cache_dir: str) -> bool:
    """The search half: text tower plus tokenizer."""
    return _present(cache_dir, _TEXT_FILES)


def models_ready(cache_dir: str) -> bool:
    """Everything downloaded — both towers and the tokenizer."""
    return _present(cache_dir, _FILES)


def download_bytes(cache_dir: str, names=None) -> int:
    """How many bytes ``ensure_models(names)`` would still have to fetch.

    For telling the user what a first run is about to cost, rather than starting
    a 372 MB download with no warning.
    """
    d = models_dir(cache_dir)
    return sum(
        size
        for n, (_rel, size, _sha) in _FILES.items()
        if (names is None or n in names)
        and not ((d / n).is_file() and (d / n).stat().st_size == size)
    )


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch(name: str, dest: Path, log=None) -> None:
    """Download one file, verify size + SHA-256, then rename into place.

    Atomic (temp + rename) so an interrupted download never leaves a truncated
    ONNX that would fail obscurely much later, and hash-checked so a corrupt or
    substituted file is refused outright rather than silently producing garbage
    vectors.
    """
    rel, size, digest = _FILES[name]
    dest.parent.mkdir(parents=True, exist_ok=True)
    if log:
        log(f"downloading search model {name} ({size / 1024 / 1024:.0f} MB) …")
    url = _HF_BASE + rel
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), suffix=".part")
    os.close(fd)
    try:
        urllib.request.urlretrieve(url, tmp)
        got = os.path.getsize(tmp)
        if got != size:
            raise OSError(f"{name}: got {got} bytes, expected {size}")
        actual = _sha256(Path(tmp))
        if actual != digest:
            raise OSError(f"{name}: sha256 {actual} does not match {digest}")
        os.replace(tmp, dest)
    except Exception as exc:
        # Wrap and re-raise with a message identifying which file/url failed;
        # the caller reports or logs it -- no log call here, to avoid recording
        # the same failure twice.
        raise OSError(f"could not download {name} from {url}: {exc}") from exc
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def ensure_models(cache_dir: str, names=None, log=None) -> Path:
    """Download the requested weights once. Returns the models directory.

    ``names`` defaults to everything; pass ``_VISION_FILES`` or ``_TEXT_FILES``
    to fetch only the half a caller actually needs.
    """
    d = models_dir(cache_dir)
    d.mkdir(parents=True, exist_ok=True)
    for name in names if names is not None else _FILES:
        target = d / name
        if target.is_file() and target.stat().st_size == _FILES[name][1]:
            continue
        _fetch(name, target, log=log)
    return d


def _session(path: Path, threads: int):
    """One onnxruntime CPU session, with the thread cap the caller asked for."""
    import onnxruntime as ort

    so = ort.SessionOptions()
    so.intra_op_num_threads = threads
    return ort.InferenceSession(str(path), so, providers=["CPUExecutionProvider"])


def default_threads() -> int:
    """One core fewer than the machine has.

    Every other backend here uses the full ``os.cpu_count()``, which is wrong for
    this stage: semantic indexing is a PARALLEL_KINDS member (gui/pipeline.py) and
    runs *concurrently* with scan and enrich for several hours. Leaving a core
    free is the difference between a background job and an unusable machine.
    """
    return max(1, (os.cpu_count() or 4) - 1)


class SiglipBackend:
    """The two SigLIP 2 towers, loaded on first use.

    Not thread-safe, like the other backends: one instance per job.

    **Preprocessing is the load-bearing part of this class.** SigLIP is trained
    with one exact recipe, and a mismatch costs retrieval quality silently, with
    no error anywhere. Both halves below are pinned against
    ``preprocessor_config.json`` / ``tokenizer.json`` of the checkpoint and
    covered by a parity test against ``transformers`` (tests/test_siglip_parity.py).

    Images (``SiglipImageProcessor``, FixRes variant):

    1. Resize to exactly 256x256 **bilinear** (``resample: 2``). No aspect
       preservation and no centre crop — SigLIP squashes, and matching
       training-time preprocessing beats looking right.
    2. ``float32 / 255`` (``rescale_factor``), then ``(x - 0.5) / 0.5`` ->
       ``[-1, 1]`` (``image_mean = image_std = [0.5, 0.5, 0.5]``).
    3. HWC -> CHW, batched to ``(N, 3, 256, 256)`` contiguous float32.

    Text (Gemma 256k SentencePiece):

    1. **Lowercase manually.** Neither ``tokenizer.json``'s normaliser nor
       transformers lowercases — the normaliser only maps spaces to U+2581 —
       yet ``do_lower_case: true`` in ``tokenizer_config.json`` records what the
       model was trained on, and cased input really does produce different ids.
    2. ``enable_truncation(64)``. The file configures padding but leaves
       truncation null, so it has to be set here.
    3. **Do not touch padding.** ``tokenizer.json`` already carries
       ``Fixed(64)`` with ``pad_id 0``; calling ``enable_padding`` with a guessed
       pad id silently replaces the correct one with a real token and corrupts
       every query.
    4. ``input_ids`` as int64 ``(N, 64)``. The text tower takes no attention mask.

    Both towers return ``pooler_output`` — *not* ``image_embeds``/``text_embeds``:
    SigLIP has no separate projection head, so the pooled output is the embedding.
    Names are read from the session rather than hardcoded anyway.
    """

    def __init__(self, cache_dir: str, *, threads: int | None = None, log=None):
        if not available():
            raise RuntimeError(
                "semantic embedding backend unavailable; install the 'semantic' "
                "extra (onnxruntime + tokenizers + Pillow + numpy)."
            )
        self.cache_dir = cache_dir
        self.threads = default_threads() if threads is None else max(1, int(threads))
        self._log = log
        self._vision = None
        self._vision_in = None
        self._vision_out = None
        self._text = None
        self._text_in = None
        self._text_out = None
        self._tokenizer = None
        # Loading is the only part that races. The GUI serves searches from a
        # ThreadingHTTPServer and warms the text tower on a background thread at
        # startup, so two threads really can reach load_text() at once; without
        # this they would each build a session and download concurrently.
        # ``run()`` itself needs no lock — onnxruntime sessions support
        # concurrent calls, and the two towers are separate sessions anyway.
        self._lock = threading.Lock()

    # -- lazy session loading ---------------------------------------------
    @staticmethod
    def _pooler_index(session) -> int:
        """Where the embedding is among the session's outputs.

        Read from the graph rather than hardcoded: these exports call it
        ``pooler_output`` (SigLIP has no separate projection head, so the pooled
        output *is* the embedding), not the ``image_embeds``/``text_embeds`` a
        CLIP-shaped export would use.
        """
        names = [o.name for o in session.get_outputs()]
        return names.index("pooler_output") if "pooler_output" in names else 0

    def load_vision(self):
        if self._vision is None:
            with self._lock:
                if self._vision is None:
                    d = ensure_models(self.cache_dir, _VISION_FILES, log=self._log)
                    session = _session(d / VISION_MODEL, self.threads)
                    self._vision_in = session.get_inputs()[0].name
                    self._vision_out = self._pooler_index(session)
                    self._vision = session
        return self._vision

    def load_text(self):
        if self._text is None:
            with self._lock:
                if self._text is None:
                    from tokenizers import Tokenizer

                    d = ensure_models(self.cache_dir, _TEXT_FILES, log=self._log)
                    session = _session(d / TEXT_MODEL, self.threads)
                    self._text_in = session.get_inputs()[0].name
                    self._text_out = self._pooler_index(session)
                    tok = Tokenizer.from_file(str(d / TOKENIZER))
                    # Truncation only. Padding is already Fixed(64) in the file —
                    # see the class docstring for why overriding it is a trap.
                    tok.enable_truncation(max_length=MAX_TOKENS)
                    self._tokenizer = tok
                    self._text = session
        return self._text

    # -- preprocessing -----------------------------------------------------
    @staticmethod
    def _pixels(image) -> np.ndarray:
        """One PIL image -> ``(3, 256, 256)`` float32 in ``[-1, 1]``."""
        from PIL import Image

        if image.mode != "RGB":
            image = image.convert("RGB")
        if image.size != (IMAGE_SIZE, IMAGE_SIZE):
            image = image.resize((IMAGE_SIZE, IMAGE_SIZE), Image.BILINEAR)
        x = np.asarray(image, dtype=np.float32) / 255.0
        x = (x - 0.5) / 0.5
        return np.ascontiguousarray(x.transpose(2, 0, 1))

    def _open(self, item) -> np.ndarray:
        from PIL import Image

        if isinstance(item, (str, Path)):
            with Image.open(item) as im:
                # No exif_transpose here: callers hand over cached thumbnails
                # from gui/thumbs.py, which already applied EXIF orientation and
                # the archive's own rotate_deg. Doing it twice would rotate a
                # sideways photo away from the way the app displays it.
                return self._pixels(im)
        if isinstance(item, np.ndarray):
            return self._pixels(Image.fromarray(item))
        return self._pixels(item)  # already a PIL image

    def _tokenize(self, texts: list[str]) -> np.ndarray:
        self.load_text()
        encoded = self._tokenizer.encode_batch([t.lower() for t in texts])
        return np.asarray([e.ids for e in encoded], dtype=np.int64)

    # -- embedding ---------------------------------------------------------
    @staticmethod
    def _normalize(vectors: np.ndarray) -> np.ndarray:
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        return np.divide(vectors, norms, out=np.zeros_like(vectors), where=norms > 0)

    def embed_images(self, items) -> np.ndarray:
        """Paths / PIL images / HWC uint8 arrays -> ``(N, 768)`` unit vectors.

        Batching buys nothing measurable on this CPU (a batch of 4 costs 4x a
        single forward), so the caller is free to pass one at a time and keep
        progress reporting fine-grained.
        """
        items = list(items)
        if not items:
            return np.zeros((0, DIMENSIONS), dtype=np.float32)
        session = self.load_vision()
        batch = np.stack([self._open(i) for i in items])
        out = session.run(None, {self._vision_in: batch})[self._vision_out]
        return self._normalize(np.asarray(out, dtype=np.float32))

    def embed_texts(self, texts) -> np.ndarray:
        """Query strings -> ``(N, 768)`` unit vectors in the image space."""
        texts = list(texts)
        if not texts:
            return np.zeros((0, DIMENSIONS), dtype=np.float32)
        session = self.load_text()
        ids = self._tokenize(texts)
        out = session.run(None, {self._text_in: ids})[self._text_out]
        return self._normalize(np.asarray(out, dtype=np.float32))

    def embed_frames_mean(self, items) -> np.ndarray | None:
        """One vector for a video: the mean of its sampled frames, renormalised.

        Averaging unit vectors and normalising again is the standard way to pool
        a clip into the frame space, and it keeps a video comparable to a photo
        under the same cosine threshold.
        """
        vectors = self.embed_images(items)
        if not len(vectors):
            return None
        mean = vectors.mean(axis=0, keepdims=True)
        normalized = self._normalize(mean)[0]
        return None if not float(np.linalg.norm(normalized)) else normalized
