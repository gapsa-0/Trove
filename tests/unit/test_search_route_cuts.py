"""The "Show only top matches" box, as the route understands it.

Description search trims weak matches with two configured cuts. The box lets a
searcher turn that off for one search and rank the whole archive instead, which
matters exactly when the cuts hid something real -- so the route has to map
``top=no`` onto *both* cuts, not just the obvious one. Leaving the relative
floor in place would silently keep trimming and the box would look broken.

Route-level on purpose: the scoring behind these two numbers is pinned in
``tests/integration/test_semantic_relevance_cut.py``. What can only go wrong
here is the translation from a query parameter to those numbers, so these tests
stub the model away and assert on what the service is asked for.
"""

from __future__ import annotations

import pytest

from organize_archive.config import Config
from organize_archive.web.routes import search as search_route
from organize_archive.web.routes._request import Request


@pytest.fixture
def captured(monkeypatch):
    """Run the handler with the model stubbed, returning the service's kwargs."""
    calls: dict = {}

    class FakeSemantic:
        INDEXER_VERSION = "test"

        @staticmethod
        def available():
            return True

        @staticmethod
        def embed_queries(cfg, queries):
            return [[1.0, 0.0] for _ in queries]

        @staticmethod
        def text_center(cfg):
            return [0.0, 0.0]

    def fake_search(db_path, vector, **kwargs):
        calls.update(kwargs)
        return {"items": [], "offset": 0, "limit": 0, "count": 0, "total": 0}

    monkeypatch.setitem(
        __import__("sys").modules, "organize_archive.services.semantic", FakeSemantic
    )
    monkeypatch.setattr(search_route.search, "semantic_search", fake_search)
    monkeypatch.setattr(search_route.search, "archive_center", lambda *a, **k: None)
    return calls


def _run(query: dict[str, list[str]], tmp_path) -> None:
    cfg = Config()
    cfg.archives = [{"id": 1, "path": str(tmp_path), "added_at": "2026-01-01"}]
    search_route.semantic_search(
        Request(
            method="GET",
            path="/api/browse/semantic/search",
            query=query,
            body={},
            cfg=cfg,
            jobs=None,
        )  # type: ignore[arg-type]
    )


def test_by_default_both_configured_cuts_are_applied(captured, tmp_path):
    _run({"root": ["1"], "q": ["a forest"]}, tmp_path)

    assert captured["min_similarity"] == pytest.approx(Config().semantic_search_min_similarity)
    assert captured["relative_floor"] == pytest.approx(Config().semantic_search_relative_floor)


def test_top_no_stands_both_cuts_down_together(captured, tmp_path):
    """Ranking the whole archive means neither cut may survive.

    -1.0 and 0.0 are the values that keep every scored row: any cosine clears
    an absolute floor of -1, and a relative floor of 0 is switched off outright.
    """
    _run({"root": ["1"], "q": ["a forest"], "top": ["no"]}, tmp_path)

    assert captured["min_similarity"] == -1.0
    assert captured["relative_floor"] == 0.0


@pytest.mark.parametrize("value", ["yes", "", "true", "1", "NO"])
def test_only_the_exact_opt_out_widens_the_search(captured, tmp_path, value):
    """Anything other than ``no`` leaves the tuned cuts alone.

    The default is the careful one, so an unrecognised value must fall back to
    it rather than quietly showing a user the whole archive.
    """
    _run({"root": ["1"], "q": ["a forest"], "top": [value]}, tmp_path)

    assert captured["min_similarity"] == pytest.approx(Config().semantic_search_min_similarity)
