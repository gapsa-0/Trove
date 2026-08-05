"""What each feature needs downloaded, and getting it here.

Three features cost a download: People (a face detector and an embedder), Pets
(a detector and a re-ID embedder) and Search by description (two SigLIP towers
and a tokenizer). Every one of those weights lives behind a different backend
with its own idea of where a file goes and how to fetch it, and until now the
only code that knew the full list was the stage that happened to need it.

This module is that list, in one table, answering the three questions anyone
asks about it: is the backend importable at all, are the weights already on
this machine, and fetch them. The setup panel asks the first two to price a
feature honestly (``services/archives.features``); the fetch job asks all three
so that pressing "Create archive" starts the download, rather than a scan that
finishes hours later and only *then* begins a 689 MB fetch nobody was waiting
by the screen for (``pipeline/runners/models.py``).

The weights are shared between archives -- they live under the app-wide cache
dir, not any archive's own (see ``paths.py``) -- so this table is a fact about
the installation, and the second archive that wants People has nothing to do.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .. import features as feature_catalog
from ..config import Config

# What a caller passes to watch a one-time model download.
Log = Callable[[str], None]


@dataclass(frozen=True)
class Weights:
    """One feature's model weights: is the backend here, are they, and fetch them."""

    available: Callable[[], bool]
    ready: Callable[[], bool]
    fetch: Callable[[Log | None], None]


def _table(cfg: Config) -> dict[str, Weights]:
    """Per-feature weights, for the features that have any.

    Imported inside the function, like every other model-backed probe in this
    package: asking whether People is available must not drag onnxruntime into
    a process that only wanted to list archives.

    A feature with no entry here needs nothing downloaded, which is why callers
    treat a missing entry as available, ready, and nothing to fetch.
    """
    from .. import model_manifest
    from ..embeddings import backend as embed_backend
    from ..faces import backend as face_backend
    from ..pets import backend as pet_backend

    cache = cfg.cache_dir

    def people(log: Log | None) -> None:
        # The manifest-resolved embedder first, in the order both backends'
        # constructors use: it is the weight that can be genuinely unobtainable,
        # and discovering that after fetching 275 MB of detector helps nobody.
        face_backend.preflight(cache)
        model_manifest.ensure(face_backend.ADAFACE_MODEL_NAME, cache, log=log)
        face_backend.ensure_models(cache, log=log)

    def pets(log: Log | None) -> None:
        pet_backend.preflight(cache)
        model_manifest.ensure(pet_backend.DINOV2_MODEL_NAME, cache, log=log)
        pet_backend.ensure_model(cache, log=log)

    def semantic(log: Log | None) -> None:
        # Both towers and the tokenizer -- the whole figure the setup screen
        # quoted. Indexing needs only the vision tower, but the first search
        # needs the other two, and a search is a request with nowhere to show a
        # 317 MB download (see services/semantic.warm_text_model).
        embed_backend.ensure_models(cache, log=log)

    return {
        "people": Weights(face_backend.available, lambda: face_backend.models_ready(cache), people),
        "pets": Weights(pet_backend.available, lambda: pet_backend.models_ready(cache), pets),
        "semantic": Weights(
            embed_backend.available, lambda: embed_backend.models_ready(cache), semantic
        ),
        # Documents and Pictures of text are deliberately absent: a feature with
        # no entry here needs nothing downloaded, which is what makes their "no
        # download" honest -- one parses, and the other's weights ship inside
        # the wheel.
    }


def available(cfg: Config, feature_id: str) -> bool:
    """Whether this feature's backend imports at all on this installation.

    Says nothing about the weights: a feature is offered *because* it can run,
    and the download is what running it costs.
    """
    weights = _table(cfg).get(feature_id)
    return bool(weights.available()) if weights else True


def ready(cfg: Config, feature_id: str) -> bool:
    """Whether this feature's weights are already on this machine."""
    weights = _table(cfg).get(feature_id)
    return bool(weights.ready()) if weights else True


def missing(cfg: Config, enabled: Iterable[str]) -> tuple[str, ...]:
    """Enabled features whose weights are not here yet, in catalogue order.

    Never touches the network, and costs a handful of ``stat`` calls: this is
    asked on every scheduler tick, and the answer has to be free once it is no.

    A feature whose backend is not installed is not reported: there is no
    download that would make it run, and its stage already reports itself
    unavailable rather than pretending to wait for one.
    """
    table = _table(cfg)
    on = set(enabled)
    return tuple(
        f.id
        for f in feature_catalog.FEATURES
        if f.id in on and f.id in table and table[f.id].available() and not table[f.id].ready()
    )


def fetch(cfg: Config, feature_id: str, log: Log | None = None) -> None:
    """Download one feature's weights, if they are not already here.

    Idempotent, and every backend behind it verifies size and SHA-256 before
    putting a file in place, so an interrupted fetch costs the current file and
    nothing else. ``log`` receives the download's own progress line.
    """
    weights = _table(cfg).get(feature_id)
    if weights:
        weights.fetch(log)
