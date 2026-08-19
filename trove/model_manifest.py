"""Model weights this project re-publishes itself: one manifest, four sources.

Most weights this app uses are fetched at first use from a stable upstream --
the OpenCV Zoo YOLOX detector, the InsightFace buffalo_l pack, the SigLIP 2
towers -- and need nothing from this module. The rest are here, for two
different reasons.

Two have no upstream at all: the AdaFace embedder and the DINOv2 pet re-ID model
exist only as self-exports (see ``tools/build/*_export.py``), so there is nowhere
to fetch them from and this manifest stands in for one.

Seven do have an upstream and are mirrored anyway, because what this file
guarantees is not availability but *exact bytes*: the three PP-OCR weights that
used to travel inside the rapidocr wheel, and the four Bergamot files that used
to sit in ``web/vendor/`` (see ADR 0019 and ``trove/translation.py``). Their
publishers can retag the paths they came from; a release asset pinned by SHA-256
here cannot move under a build that already referenced it.

Either way the entry records the file's size, SHA-256, provenance, licence, and
our own re-publication as a release asset.

That manifest used to be readable only by ``packaging/scripts/stage-models.py``,
which meant a frozen build carried these files and *every other way of
running the app* -- ``npm run dev``, ``trove``, any source checkout -- had no path
to them at all. The visible symptom was detection downloading ~310 MB of the
weights it could fetch and then failing on the one it could not. So the manifest
is a runtime contract now, and every entry resolves identically everywhere:

1. ``ARCHIVE_MODELS_DIR``, or a frozen build's bundle (``runtime.bundled_model``)
2. ``packaging/models/staged/`` in a source checkout -- whatever a local
   ``stage-models.py`` run already produced, so a developer who has built a
   package never downloads these twice
3. the download cache, ``<cache_dir>/models/<file>`` -- where 4 puts them
4. the manifest ``url``, downloaded once, size- and SHA-256-verified

Only step 4 touches the network, and only for a file that is genuinely absent.
``obtainable()`` answers "could this be resolved at all" *without* the network,
which is what lets a caller refuse a run before spending bandwidth on the other
half of its models (see ``detect/extract.make_backends``).

Packaging imports this module rather than reimplementing it, so the schema, the
hashes and the verification have exactly one implementation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path

from . import runtime
from .errors import ModelUnavailableError

# What a caller passes to watch a one-time model download.
Log = Callable[[str], None]

# What to call a set of weights in front of somebody.
#
# The manifest keys are file names -- "adaface", "ppocr_rec", "bergamot_es_en_lex"
# -- and they were going straight onto the sidebar chip, so a new archive's
# first minutes read "Downloading adaface model: 7% of 249 MB". Nobody outside
# this repo knows what an adaface is, and it is the first thing Trove ever says.
# A reader wants to know which feature is being got ready, which is the one
# thing the file name cannot tell them.
_MODEL_WORDS = {
    "adaface": "the face recogniser",
    "dinov2_pet": "the pet recogniser",
    "ppocr_det": "the picture-text reader",
    "ppocr_rec": "the picture-text reader",
    "ppocr_cls": "the picture-text reader",
    "bergamot_wasm": "the translator",
    "bergamot_es_en_model": "the translator",
    "bergamot_es_en_lex": "the translator",
    "bergamot_es_en_vocab": "the translator",
}


def model_words(name: str) -> str:
    """A weights set named for what it lets Trove do, not for its file."""
    return _MODEL_WORDS.get(name, f"the {name} model")


logger = logging.getLogger(__name__)

# The repo root, resolved the same way config.settings does it: this file is
# trove/model_manifest.py, so two parents up is the checkout. Absent
# in a frozen build (where step 1 answers) and in a wheel installed outside a
# checkout (where the error message below is what the user gets).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = PROJECT_ROOT / "packaging" / "models" / "manifest.json"
STAGED_DIR = PROJECT_ROOT / "packaging" / "models" / "staged"

SCHEMA_VERSION = 1
_REQUIRED_TEXT_KEYS = ("name", "file", "sha256", "source", "license")


def download_progress(
    log: Log | None, label: str, total: int, *, interval: float = 1.0
) -> Callable[[int, int, int], None] | None:
    """A ``urlretrieve`` reporthook that reports percent complete through ``log``.

    Since the weights stopped travelling inside the installer, first run fetches
    ~570 MB. ``log`` writes ``job.current``, which is the single line the stage
    card shows, so without this a user watches ``downloading adaface model
    (249 MB) …`` sit motionless for minutes and reasonably concludes it has hung.

    Throttled by wall-clock rather than by block count: ``urlretrieve`` calls back
    every 8 KB, which on a 249 MB file is thirty thousand callbacks, and each one
    of those would be a write the GUI then polls. One update a second is legible
    and costs nothing. Repeated messages are suppressed too, so a stalled download
    stops repainting rather than looking like progress.

    ``total`` is the size a caller already knows, and 0 for the two downloads
    nobody records a size for (the buffalo_l pack, the YOLOX detector) -- there
    the server's Content-Length answers, and its absence leaves a running
    megabyte count, which is still the difference between moving and hung.

    Returns None when there is nobody to report to, which is exactly what
    ``urlretrieve(..., reporthook=None)`` wants, so callers need no branch.
    """
    if log is None:
        return None
    # `nonlocal` rather than the usual dict-as-mutable-cell: a dict holding a
    # float and a str infers as dict[str, object], which makes the subtraction
    # below a type error the moment anything checks this function's body.
    said_at, said = 0.0, ""

    def hook(blocks: int, block_size: int, size: int) -> None:
        nonlocal said_at, said
        # `size` is the server's Content-Length, or -1 when it declines to say.
        # The manifest knows the answer either way, so prefer whichever is real
        # -- including for the figure quoted, or a caller that passed no size
        # would announce a percentage "of 0 MB".
        expected = size if size > 0 else total
        done = blocks * block_size
        if expected > 0:
            # The last block is short, and a stray over-100% reads as a bug.
            done = min(done, expected)
            megabytes = expected / 1024 / 1024
            message = f"downloading {label}: {done * 100 // expected}% of {megabytes:.0f} MB"
        else:
            message = f"downloading {label}: {done / 1024 / 1024:.0f} MB"
        now = time.monotonic()
        if now - said_at < interval or message == said:
            return
        said_at, said = now, message
        log(message)

    return hook


def sha256(path: Path) -> str:
    """Hex digest of a file, read in chunks so a 350 MB model is not resident."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate(item: object) -> dict:
    """One manifest entry, checked hard enough that later code can trust it.

    Every field this module and the packaging script rely on is verified here --
    including that ``file`` is a relative path with no ``..`` in it, since it is
    joined onto the cache directory and onto the staging directory.
    """
    if not isinstance(item, dict):
        raise ValueError("invalid model entry")
    for key in _REQUIRED_TEXT_KEYS:
        if not isinstance(item.get(key), str) or not item[key]:
            raise ValueError(f"model entry missing {key}")
    relative = Path(item["file"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe model path: {item['file']}")
    digest = item["sha256"]
    if len(digest) != 64 or any(c not in "0123456789abcdefABCDEF" for c in digest):
        raise ValueError(f"invalid SHA-256 for model {item['name']}")
    if not isinstance(item.get("size"), int) or item["size"] <= 0:
        raise ValueError(f"invalid size for model {item['name']}")
    url = item.get("url")
    if url is not None and (not isinstance(url, str) or not url.startswith("https://")):
        raise ValueError(f"invalid url for model {item['name']}")
    return item


def load(manifest_path: Path | None = None) -> list[dict]:
    """Every manifest entry, validated. Raises on a malformed manifest.

    ``OSError`` (no manifest here), ``ValueError`` and ``JSONDecodeError`` all
    reach the caller unwrapped: packaging wants to print them, while the app
    calls ``entry()`` below, which turns them into a user-facing error.
    """
    path = MANIFEST_PATH if manifest_path is None else manifest_path
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != SCHEMA_VERSION or not isinstance(data.get("models"), list):
        raise ValueError(f"invalid {path} schema")
    return [_validate(item) for item in data["models"]]


def entry(name: str) -> dict:
    """One entry by name, as a user-facing error when it cannot be read.

    A missing manifest is not a bug: it is what an app installed outside a
    source checkout looks like, and the honest answer there is that this weight
    must come from a packaged build or ``ARCHIVE_MODELS_DIR``.
    """
    try:
        items = load()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ModelUnavailableError(
            f"cannot read the model manifest at {MANIFEST_PATH} ({exc}). Install a "
            "packaged build, or point ARCHIVE_MODELS_DIR at a directory holding "
            "the model files."
        ) from exc
    for item in items:
        if item["name"] == name:
            return item
    raise ModelUnavailableError(f"no model named {name!r} in {MANIFEST_PATH}")


def cache_path(name: str, cache_dir: str) -> Path:
    """Where a downloaded copy lives: ``<cache_dir>/models/<file>``."""
    # Annotated rather than returned directly: a manifest entry is parsed JSON,
    # so its values are Any and warn_return_any would let one out as a Path.
    path: Path = Path(cache_dir) / "models" / entry(name)["file"]
    return path


def present(name: str, cache_dir: str) -> Path | None:
    """The first complete copy among sources 1-3, or None. Never downloads.

    Size is the completeness check, as everywhere else in this codebase: a
    truncated file must not be mistaken for a usable one. The full SHA-256 is
    verified when a file is *downloaded*, not on every startup -- hashing 350 MB
    to answer "is the model there" would cost seconds on every run.

    An unreadable manifest answers None rather than raising, because None is the
    honest answer to the question actually being asked: with no manifest there is
    no path to check, so this weight is certainly not here. Every caller asks it
    as a yes/no -- ``models.missing``, each backend's ``models_ready``,
    ``translation.resolve`` -- and one that raised turned a build shipped without
    its manifest into a dead scheduler rather than a feature reporting that it
    needs a download. The error is not swallowed, only deferred to the place that
    can act on it: ``ensure`` re-reads the entry and raises there, on a job whose
    failure lands on a card with the message in it.
    """
    try:
        item = entry(name)
    except ModelUnavailableError:
        return None
    candidates = (
        runtime.bundled_model(item["file"]),
        STAGED_DIR / item["file"],
        Path(cache_dir) / "models" / item["file"],
    )
    for candidate in candidates:
        if (
            candidate is not None
            and candidate.is_file()
            and candidate.stat().st_size >= item["size"]
        ):
            return candidate
    return None


def path(name: str, cache_dir: str) -> Path:
    """Where this model is, or where a download would put it.

    For callers that want a path to show the user (or to hand to onnxruntime
    after their own readiness check) rather than a fetch.
    """
    return present(name, cache_dir) or cache_path(name, cache_dir)


def obtainable(name: str, cache_dir: str) -> bool:
    """Could ``ensure`` succeed? Answered without touching the network.

    False means "no copy anywhere and no URL to get one from" -- a condition no
    amount of downloading fixes, and the one worth refusing a run over before it
    starts fetching anything else.
    """
    try:
        return present(name, cache_dir) is not None or bool(entry(name).get("url"))
    except ModelUnavailableError:
        return False


def missing_reason(name: str, cache_dir: str, *, feature: str) -> str | None:
    """Why ``feature`` cannot run, or None if it can. No network, no downloads.

    The text is the whole point: it names the file, the tool that regenerates it
    and the packaged build that ships it, because this message is what reaches
    the user on a status card.
    """
    if obtainable(name, cache_dir):
        return None
    try:
        item = entry(name)
    except ModelUnavailableError as exc:
        return f"{feature} needs the {name} model: {exc}"
    tool = item["source"].split(" —")[0]
    return (
        f"{feature} needs {item['file']}, which is not on this machine and has no "
        f"download URL in the manifest. Regenerate it with `python3 {tool}` "
        "(dev-only: needs torch), or install a packaged build, which ships it."
    )


def ensure(name: str, cache_dir: str, log: Log | None = None) -> Path:
    """Resolve ``name``, downloading it once if that is the only source left.

    Atomic (temp + rename) so an interrupted download never leaves a truncated
    ONNX to fail obscurely later, and both size and SHA-256 are verified against
    the manifest before the file is put in place -- a corrupt or substituted
    model is refused rather than left to produce silently meaningless vectors.
    """
    found = present(name, cache_dir)
    if found is not None:
        return found
    item = entry(name)
    url = item.get("url")
    if not url:
        raise ModelUnavailableError(missing_reason(name, cache_dir, feature=name) or name)

    destination = cache_path(name, cache_dir)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if log:
        log(f"downloading {model_words(name)} ({item['size'] / 1024 / 1024:.0f} MB) …")
    logger.info("downloading %s from %s", name, url)
    fd, tmp = tempfile.mkstemp(dir=str(destination.parent), suffix=".part")
    os.close(fd)
    try:
        urllib.request.urlretrieve(
            url, tmp, reporthook=download_progress(log, model_words(name), item["size"])
        )
        got = os.path.getsize(tmp)
        if got != item["size"]:
            raise OSError(f"got {got} bytes, expected {item['size']}")
        actual = sha256(Path(tmp))
        if actual != item["sha256"].lower():
            raise OSError(f"sha256 {actual} does not match the manifest")
        os.replace(tmp, destination)
    except Exception as exc:
        # Wrapped so the message says which model and which URL failed; the
        # caller reports it once (never logged here as well, or a single
        # failure is recorded twice).
        raise ModelUnavailableError(f"could not download {name} from {url}: {exc}") from exc
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return destination
