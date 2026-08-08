"""Serving the two kinds of vendored asset the app has.

``/vendor/`` used to answer out of one directory. It now answers out of two,
because the translator's four large files stopped travelling inside the package
and became manifest entries fetched with Search by description (ADR 0019, and
``trove/translation.py``). The route is where that split becomes visible, so it
is where the three outcomes are pinned: a package file, a downloaded file, and
the 404 that is what "not downloaded yet" looks like.

The handler is called directly rather than over a socket -- ``Request`` is a
plain dataclass and a handler never touches the response, which is the whole
reason routes are testable without a server (see ``routes/_request.py``).
"""

from __future__ import annotations

import pytest

from trove import model_manifest, translation
from trove.web.routes import static
from trove.web.routes._request import NOT_FOUND, FileBody, Request

FETCHED = "translate-es-en-model.bin"


def _request(name, cache_dir):
    class _Cfg:
        pass

    cfg = _Cfg()
    cfg.cache_dir = str(cache_dir)
    return Request("GET", f"/vendor/{name}", {}, {}, cfg, None)


def test_a_package_file_is_served_from_the_package(tmp_path):
    """Leaflet and the Bergamot loader scripts are small and still shipped."""
    result = static.vendor(_request("leaflet.css", tmp_path))
    assert isinstance(result, FileBody)
    assert result.path.name == "leaflet.css"
    assert result.path.is_file()


def test_a_fetched_file_is_served_from_the_model_cache(tmp_path, monkeypatch):
    """The four large ones are not in the package at all, so if this resolved
    against the vendor directory it would 404 on a correctly installed app."""
    cached = tmp_path / "models" / "bergamot" / FETCHED
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"x" * model_manifest.entry("bergamot_es_en_model")["size"])
    # Otherwise a developer machine with a staged copy answers from there and
    # the test would pass without the cache tier working at all.
    monkeypatch.setattr(model_manifest, "STAGED_DIR", tmp_path / "nothing-staged")
    monkeypatch.delenv("ARCHIVE_MODELS_DIR", raising=False)

    result = static.vendor(_request(FETCHED, tmp_path))
    assert isinstance(result, FileBody)
    assert result.path == cached


def test_a_fetched_file_that_is_not_here_yet_is_a_plain_404(tmp_path, monkeypatch):
    """Not an error state. Translation improves recall and is never required:
    the page catches the failed load and leaves the query unexpanded, which is
    exactly what a user who has not enabled Search by description should get."""
    monkeypatch.setattr(model_manifest, "STAGED_DIR", tmp_path / "nothing-staged")
    monkeypatch.delenv("ARCHIVE_MODELS_DIR", raising=False)

    assert static.vendor(_request(FETCHED, tmp_path)) is NOT_FOUND


def test_a_name_that_is_neither_is_a_404(tmp_path):
    assert static.vendor(_request("not-a-real-asset.bin", tmp_path)) is NOT_FOUND


@pytest.mark.parametrize("name", sorted(translation.MODELS.values()))
def test_every_fetched_name_is_one_the_route_knows_how_to_resolve(name, tmp_path):
    """The manifest, the page's registry and this route have to agree on four
    filenames. They are spelled in three places -- manifest.json, the vendor
    registry JSON, and translation.MODELS -- so a rename that misses one would
    otherwise show up as a translator that silently never loads."""
    assert translation.BY_FILENAME[name] in {e["name"] for e in model_manifest.load()}
