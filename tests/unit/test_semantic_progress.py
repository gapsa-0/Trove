"""Who a download reports to, and when a warm-up is allowed to start one.

Both properties here were broken by the same design: the SigLIP backend is a
process-wide singleton, and it used to capture the progress callback of whoever
constructed it *first*. That was always the server's startup warm-up thread,
which passes no callback — so the semantic stage's own callback was discarded
and the card stayed blank through a 372 MB vision-tower download. The callback
is a per-call argument of ``load_vision``/``load_text`` now, which is what makes
construction order stop mattering.

The warm-up itself is the second half: warming a model must never *fetch* one.
A background thread at server start has no card to report a 317 MB download on
and nowhere to surface its failure, so it returns early unless the weights are
already on disk and lets the first search (or the stage) do the fetching.

No weights and no network: the backend is a stand-in whose ``load_*`` methods
just call the callback they were handed.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest

from organize_archive.embeddings import backend as eb
from organize_archive.pipeline.runners import semantic as semantic_runner
from organize_archive.services import semantic


class FakeBackend:
    """Records the callbacks it is given, and reports like the real one does."""

    def __init__(self, cache_dir, *, threads=None):
        self.cache_dir = cache_dir
        self.vision_logs: list[str] = []
        self.text_loaded = False

    def load_vision(self, log=None):
        if log:
            log("downloading search model vision_model.onnx (372 MB) …")
            self.vision_logs.append("reported")
        return object()

    def load_text(self, log=None):
        self.text_loaded = True
        return object()


@pytest.fixture
def fake_backend(monkeypatch, tmp_path):
    monkeypatch.setattr(semantic, "_backend", None)
    monkeypatch.setattr(semantic.eb, "SiglipBackend", FakeBackend)
    monkeypatch.setattr(semantic, "available", lambda: True)
    return SimpleNamespace(cache_dir=str(tmp_path))


def _context(cfg):
    """The three attributes ``_warm_vision_model`` actually uses."""
    job = SimpleNamespace(current=None)
    return SimpleNamespace(
        cfg=cfg,
        job=job,
        uninterruptible=lambda _what: contextlib.nullcontext(),
    )


def test_the_stage_still_reports_progress_when_the_backend_was_warmed_first(fake_backend):
    """The exact regression: pre-warming used to silence the semantic card.

    ``warm_text_model`` constructs the singleton with no callback. The stage
    then asks the *same* instance to load the vision tower, and its progress
    must still reach the job — i.e. the callback cannot live on the instance.
    """
    semantic.warm_text_model(fake_backend)  # no-op download-wise, constructs nothing
    semantic.backend(fake_backend)  # whoever gets there first, gets no callback
    ctx = _context(fake_backend)

    semantic_runner._warm_vision_model(ctx)

    assert ctx.job.current == "downloading search model vision_model.onnx (372 MB) …"


def test_backend_takes_no_progress_callback(fake_backend):
    """A callback on the singleton is process-wide state; refuse to reintroduce it."""
    with pytest.raises(TypeError):
        semantic.backend(fake_backend, log=print)


def test_the_startup_warmup_never_downloads(fake_backend, monkeypatch):
    """Weights absent: warming returns without loading, so nothing is fetched."""
    monkeypatch.setattr(eb, "text_ready", lambda _cache: False)

    semantic.warm_text_model(fake_backend)

    assert semantic._backend is None


def test_the_startup_warmup_still_warms_what_is_already_downloaded(fake_backend, monkeypatch):
    """Its whole purpose survives: the first search must not wait on a load."""
    monkeypatch.setattr(eb, "text_ready", lambda _cache: True)

    semantic.warm_text_model(fake_backend)

    assert semantic._backend is not None
    assert semantic._backend.text_loaded
