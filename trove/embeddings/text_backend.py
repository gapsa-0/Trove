"""Local text embedder for document passages: multilingual-e5-small, on onnxruntime CPU.

**There are two embedders in this package, and that is not an accident.**
``backend.py`` holds SigLIP 2, whose text tower exists to project a *caption*
into image space so a cosine against a photo means something: ``MAX_TOKENS = 64``,
and a contrastive image-text space in which a page of prose has no useful
position at all. Feed it a tax letter and every tax letter lands in the same
place. Searching documents by meaning therefore needs its own model, its own
vector space and its own table, and the two are never scored against each other
(ADR 0018).

This one is ``intfloat/multilingual-e5-small``: MIT, 384-d, a 512-token window,
100 languages, and an official ONNX export in the model repository itself. It
adds no Python dependency -- ``onnxruntime``, ``numpy`` and ``tokenizers`` are
already the ``semantic`` extra, and e5's XLM-R SentencePiece vocabulary loads
through the same ``tokenizers``.

**Three things here are silent when wrong**, which is why each is spelled out
where it happens rather than left to the reader: the asymmetric prefixes, the
pooling, and the token budget. There is no error for getting any of them wrong,
only worse results.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import threading
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from .. import model_manifest
from ..errors import ModelUnavailableError

if TYPE_CHECKING:
    from tokenizers import Tokenizer

Session = Any
Log = Callable[[str], None]

logger = logging.getLogger(__name__)

try:
    import numpy as np
except Exception:  # pragma: no cover - optional dep
    logger.debug("numpy import failed; text embedder will report unavailable", exc_info=True)
    np = None  # type: ignore[assignment]

# Identity of the vector space ``doc_chunk_embeddings`` holds. BUMP THIS whenever
# the model, the prefixes, the pooling or the quantisation below changes: every
# stored vector was produced by a different function and is not comparable to a
# fresh query, so the archive re-embeds itself.
EMBEDDER_VERSION = "e5-small-multilingual-int8-v1"

DIMENSIONS = 384
# The model's hard window. Passages longer than this are split rather than
# truncated -- see ``embed_passages``.
MAX_TOKENS = 512

# e5 is trained asymmetrically, and these are not decoration. A passage embedded
# without its prefix, or a query embedded with the wrong one, lands in a
# measurably different part of the space -- retrieval simply gets worse, with
# nothing to indicate why.
QUERY_PREFIX = "query: "
PASSAGE_PREFIX = "passage: "

MODELS_SUBDIR = "e5-small"
TEXT_MODEL = "model_int8.onnx"
TOKENIZER = "tokenizer.json"

# Pinned revision of intfloat/multilingual-e5-small. Pinned rather than "main"
# so a repo update can never silently swap the vector space under an already
# indexed archive.
_HF_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
_HF_BASE = f"https://huggingface.co/intfloat/multilingual-e5-small/resolve/{_HF_REVISION}/"

# name on disk -> (path within the HF repo, exact size, sha256)
#
# The int8 export rather than the 470 MB fp32 one, and that needed checking
# rather than assuming: it is quantised U8S8, which on x86 without AVX512-VNNI
# can saturate and lose accuracy. Measured on an AVX2-only machine against the
# fp32 weights over the same passages: identical ranking, largest similarity
# difference 0.0044, per-vector cosine never below 0.9949. The saturation
# concern does not materialise, and a quarter of the download does.
_FILES: dict[str, tuple[str, int, str]] = {
    TEXT_MODEL: (
        "onnx/model_qint8_avx512_vnni.onnx",
        118_346_824,
        "dd476dd0c2514e9b9be83aeb3853fac0763e0bdf4a71645407587d77c48a2d88",
    ),
    TOKENIZER: (
        "onnx/tokenizer.json",
        17_082_730,
        "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39",
    ),
}


def available() -> bool:
    """True if onnxruntime, tokenizers and numpy are importable.

    Deliberately does *not* check for downloaded weights, for the reason
    ``backend.available`` gives: this decides whether the pipeline offers the
    stage, and the stage is what downloads them.
    """
    if np is None:
        return False
    try:
        import onnxruntime  # noqa: F401
        import tokenizers  # noqa: F401

        return True
    except Exception:  # pragma: no cover - optional dep
        logger.debug(
            "onnxruntime/tokenizers unavailable; search by meaning disabled", exc_info=True
        )
        return False


def models_dir(cache_dir: str) -> Path:
    return Path(cache_dir) / "models" / MODELS_SUBDIR


def models_ready(cache_dir: str) -> bool:
    """Both files present at their exact sizes."""
    d = models_dir(cache_dir)
    return all((d / n).is_file() and (d / n).stat().st_size == _FILES[n][1] for n in _FILES)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch(name: str, dest: Path, log: Log | None = None) -> None:
    """Download one file, verify size + SHA-256, then rename into place.

    Atomic and hash-checked for the reasons ``backend._fetch`` gives: an
    interrupted download must not leave a truncated ONNX that fails obscurely
    much later, and a substituted file must be refused rather than silently
    producing garbage vectors.
    """
    rel, size, digest = _FILES[name]
    dest.parent.mkdir(parents=True, exist_ok=True)
    if log:
        log(f"downloading the meaning model {name} ({size / 1024 / 1024:.0f} MB) …")
    url = _HF_BASE + rel
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), suffix=".part")
    os.close(fd)
    try:
        urllib.request.urlretrieve(
            url,
            tmp,
            reporthook=model_manifest.download_progress(log, f"meaning model {name}", size),
        )
        got = os.path.getsize(tmp)
        if got != size:
            raise OSError(f"{name}: got {got} bytes, expected {size}")
        actual = _sha256(Path(tmp))
        if actual != digest:
            raise OSError(f"{name}: sha256 {actual} does not match {digest}")
        os.replace(tmp, dest)
    except Exception as exc:
        raise OSError(f"could not download {name} from {url}: {exc}") from exc
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def ensure_models(cache_dir: str, log: Log | None = None) -> Path:
    """Download the weights once. Returns the models directory."""
    d = models_dir(cache_dir)
    d.mkdir(parents=True, exist_ok=True)
    for name in _FILES:
        target = d / name
        if target.is_file() and target.stat().st_size == _FILES[name][1]:
            continue
        _fetch(name, target, log=log)
    return d


def default_threads() -> int:
    """Half the machine's cores, not all of them and not all-but-one.

    This stage can be one of five running at once -- scan, enrich, semantic,
    text and this -- and three of those hold ONNX sessions. The one-core-free
    rule ``backend.default_threads`` uses assumes it is the only heavy job;
    here it would let two embedders between them ask for twice the machine.
    """
    return max(1, (os.cpu_count() or 4) // 2)


class E5Backend:
    """The e5 encoder, loaded on first use. Not thread-safe: one per job.

    **The recipe below is the load-bearing part**, and every step of it fails
    quietly rather than loudly.

    1. **Prefixes.** ``passage: `` on stored text, ``query: `` on a search. e5
       is trained asymmetrically; using one prefix for both, or neither, costs
       retrieval quality with no signal that anything is wrong.
    2. **Masked mean pooling, then L2 normalise.** e5 has no pooler output and
       no CLS-token head -- do not reach for ``backend.py``'s pooler index. The
       session returns ``last_hidden_state``, and the mean must be taken over
       the attention mask, not over the padded width, or every short passage is
       averaged towards zero by its own padding.
    3. **The token budget is enforced here, not by the tokenizer.** Handing an
       over-length passage to ``encode`` truncates it in silence, and the tail
       of the passage becomes unsearchable with nothing recorded anywhere. See
       ``embed_passages``.
    """

    def __init__(self, cache_dir: str, threads: int | None = None) -> None:
        if not available():
            raise ModelUnavailableError(
                "search by meaning needs the local text model; install the "
                "'semantic' extra (onnxruntime + tokenizers + numpy)."
            )
        self.cache_dir = cache_dir
        self.threads = default_threads() if threads is None else max(1, int(threads))
        self._session: Session | None = None
        self._inputs: frozenset[str] = frozenset()
        self._tokenizer: Tokenizer | None = None
        self._lock = threading.Lock()

    # -- loading ------------------------------------------------------------

    def load(self, log: Log | None = None) -> None:
        """Fetch the weights if absent and open the session. Idempotent."""
        with self._lock:
            if self._session is not None:
                return
            import onnxruntime as ort
            from tokenizers import Tokenizer as HFTokenizer

            d = ensure_models(self.cache_dir, log=log)
            so = ort.SessionOptions()
            so.intra_op_num_threads = self.threads
            session = ort.InferenceSession(
                str(d / TEXT_MODEL), so, providers=["CPUExecutionProvider"]
            )
            # Read the input names off the session rather than hardcoding them.
            # Exports of this model differ in whether they take token_type_ids,
            # and feeding a name the graph does not declare is a hard error
            # while omitting one it needs is a wrong answer.
            self._inputs = frozenset(i.name for i in session.get_inputs())
            self._session = session
            self._tokenizer = HFTokenizer.from_file(str(d / TOKENIZER))

    def _ready(self) -> tuple[Session, Tokenizer]:
        if self._session is None or self._tokenizer is None:
            self.load()
        assert self._session is not None and self._tokenizer is not None
        return self._session, self._tokenizer

    # -- encoding -----------------------------------------------------------

    def _forward(self, batch: list[list[int]]) -> Any:
        """One padded batch of token ids through the session, pooled and normalised."""
        session, _ = self._ready()
        width = max(len(ids) for ids in batch)
        input_ids = np.zeros((len(batch), width), dtype=np.int64)
        mask = np.zeros((len(batch), width), dtype=np.int64)
        for row, ids in enumerate(batch):
            input_ids[row, : len(ids)] = ids
            mask[row, : len(ids)] = 1
        feed = {"input_ids": input_ids, "attention_mask": mask}
        if "token_type_ids" in self._inputs:
            feed["token_type_ids"] = np.zeros_like(input_ids)
        hidden = session.run(None, {k: v for k, v in feed.items() if k in self._inputs})[0]
        # Masked mean: padding must contribute nothing, or a short passage is
        # pulled towards zero by however much padding its batch happened to need
        # -- which makes a vector depend on the company it was encoded with.
        weights = mask[..., None].astype(np.float32)
        pooled = (hidden * weights).sum(axis=1) / np.maximum(weights.sum(axis=1), 1e-9)
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        return pooled / np.maximum(norms, 1e-12)

    def _windows(self, text: str, prefix: str) -> list[list[int]]:
        """Token ids for one text, split into as many windows as it needs.

        The split is the whole reason this function exists. ``chunk.py`` sizes
        passages in characters and cannot bound tokens -- measured against this
        very tokenizer, a 1195-character chunk of CSV rows is 628 tokens where
        the same length of prose is 262, and the ratio has no floor. Letting the
        tokenizer truncate would silently drop the tail of exactly the dense,
        numeric passages a paperwork archive is full of.
        """
        _, tokenizer = self._ready()
        ids = tokenizer.encode(prefix + text).ids
        if len(ids) <= MAX_TOKENS:
            return [ids]
        # Keep the prefix on every window: each one is embedded as a passage in
        # its own right, and a window missing it sits somewhere else entirely.
        head = tokenizer.encode(prefix).ids
        body = ids[len(head) :]
        room = MAX_TOKENS - len(head)
        return [head + body[at : at + room] for at in range(0, len(body), room)]

    def embed_passages(self, texts: list[str], batch_size: int = 16) -> list[list[float]]:
        """One vector per passage, in the order given.

        A passage too long for the window is embedded as several and averaged
        back into one vector, so the caller still gets exactly one per input and
        ``doc_chunk_embeddings.chunk_id`` stays a primary key. Averaging is the
        conservative choice: it keeps every part of the passage represented,
        where truncating discards the tail outright.
        """
        if not texts:
            return []
        windows: list[list[int]] = []
        owners: list[int] = []
        for index, text in enumerate(texts):
            for window in self._windows(text, PASSAGE_PREFIX):
                windows.append(window)
                owners.append(index)

        encoded = [
            self._forward(windows[at : at + batch_size])
            for at in range(0, len(windows), batch_size)
        ]
        stacked = np.concatenate(encoded, axis=0)

        out = np.zeros((len(texts), DIMENSIONS), dtype=np.float32)
        for row, owner in enumerate(owners):
            out[owner] += stacked[row]
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        # ndarray.tolist() is typed Any, which the return annotation would
        # otherwise swallow; the array is float32 of the declared width here.
        return cast(list[list[float]], (out / np.maximum(norms, 1e-12)).tolist())

    def embed_queries(self, queries: list[str]) -> list[list[float]]:
        """One vector per query. Long queries are truncated, and that is fine --
        nobody types 512 tokens into a search box, and unlike a passage there is
        no stored tail to lose."""
        if not queries:
            return []
        _, tokenizer = self._ready()
        batch = [tokenizer.encode(QUERY_PREFIX + q).ids[:MAX_TOKENS] for q in queries]
        return cast(list[list[float]], self._forward(batch).tolist())
