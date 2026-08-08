"""The manifest as a runtime contract, not just a packaging input.

Two weights here — the AdaFace embedder and the DINOv2 pet re-ID model — have no
upstream URL, so ``packaging/models/manifest.json`` is the only description of
where they come from. For a long time only the packaging script could read it,
which meant a frozen build carried the files and every source checkout had *no*
way to obtain them: `npm run dev` downloaded ~310 MB of the other weights and
then failed on these. These tests hold the fixed contract:

* every manifest entry resolves through the *application's* resolver, so
  packaging and runtime can never disagree about where a model lives;
* a download is size- and hash-verified, and a mismatch is refused rather than
  written into the cache;
* when a model genuinely cannot be obtained, the failure arrives before anything
  is downloaded, and its message names the tool that regenerates it.

Nothing here touches the network: the download tier is exercised with a stubbed
``urlretrieve``, and the files are a few bytes each.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from trove import model_manifest as mm
from trove.errors import ModelUnavailableError

CONTENT = b"not really an onnx file, but it hashes like one"
DIGEST = hashlib.sha256(CONTENT).hexdigest()


@pytest.fixture
def manifest(tmp_path, monkeypatch):
    """A one-entry manifest with a real hash, in place of the repo's."""
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "models": [
                    {
                        "name": "adaface",
                        "file": "adaface/adaface_ir101_w12m.onnx",
                        "sha256": DIGEST,
                        "size": len(CONTENT),
                        "source": "tools/build/adaface_export.py — fp32 ONNX export",
                        "license": "see THIRD_PARTY_NOTICES",
                        "url": "https://example.invalid/adaface.onnx",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(mm, "MANIFEST_PATH", path)
    # The staged directory is a repo path; point it somewhere empty so a
    # developer machine that has actually staged models cannot satisfy a test
    # that is about the cache or the download.
    monkeypatch.setattr(mm, "STAGED_DIR", tmp_path / "staged")
    monkeypatch.delenv("ARCHIVE_MODELS_DIR", raising=False)
    return path


def test_the_repos_own_manifest_resolves_through_the_app(tmp_path):
    """Packaging's input and the app's resolver read the same file, validated.

    The regression this guards is structural: the manifest used to be parsed
    only by packaging/scripts/stage-models.py, so nothing noticed that the
    application had no code path to these weights at all.
    """
    entries = mm.load()
    assert {e["name"] for e in entries} == {
        # The two self-exports, which have no upstream to fetch from...
        "adaface",
        "dinov2_pet",
        # ...the three PP-OCR weights, which do, and are mirrored anyway so the
        # bytes stay pinned to a release this project controls (ADR 0019)...
        "ppocr_det",
        "ppocr_rec",
        "ppocr_cls",
        # ...and the four the browser fetches rather than Python: the Spanish
        # translator the search box runs before embedding a query.
        "bergamot_wasm",
        "bergamot_es_en_model",
        "bergamot_es_en_lex",
        "bergamot_es_en_vocab",
    }
    for entry in entries:
        # Every entry must have a fetchable origin, or a source checkout is back
        # to the failure this whole module exists to prevent.
        assert entry["url"], f"{entry['name']} has no download URL"
        assert mm.obtainable(entry["name"], str(tmp_path))
        assert mm.path(entry["name"], str(tmp_path)).name == entry["file"].split("/")[-1]


def test_a_bundled_copy_wins_over_a_download(manifest, tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    (bundle / "adaface").mkdir(parents=True)
    (bundle / "adaface" / "adaface_ir101_w12m.onnx").write_bytes(CONTENT)
    monkeypatch.setenv("ARCHIVE_MODELS_DIR", str(bundle))
    monkeypatch.setattr(mm.urllib.request, "urlretrieve", _forbidden)

    assert mm.ensure("adaface", str(tmp_path / "cache")) == bundle / "adaface" / (
        "adaface_ir101_w12m.onnx"
    )


def test_a_staged_checkout_copy_is_used_before_downloading(manifest, tmp_path, monkeypatch):
    """`stage-models.py` output counts as a source, so a dev fetches once."""
    staged = tmp_path / "staged"
    (staged / "adaface").mkdir(parents=True)
    (staged / "adaface" / "adaface_ir101_w12m.onnx").write_bytes(CONTENT)
    monkeypatch.setattr(mm.urllib.request, "urlretrieve", _forbidden)

    assert mm.ensure("adaface", str(tmp_path / "cache")).parent.parent == staged


def test_a_download_lands_in_the_cache_and_reports_progress(manifest, tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    monkeypatch.setattr(mm.urllib.request, "urlretrieve", _writes(CONTENT))
    messages: list[str] = []

    got = mm.ensure("adaface", str(cache), log=messages.append)

    assert got == cache / "models" / "adaface" / "adaface_ir101_w12m.onnx"
    assert got.read_bytes() == CONTENT
    assert messages and "downloading" in messages[0]


def test_progress_is_reported_as_percent_while_a_download_runs(manifest, tmp_path, monkeypatch):
    """The stage card shows one line; it has to move on a 249 MB download."""
    monkeypatch.setattr(mm.urllib.request, "urlretrieve", _writes(CONTENT))
    # interval=0 so the throttle never suppresses anything in a test that
    # completes in microseconds; the throttle itself is asserted separately.
    monkeypatch.setattr(mm, "download_progress", _unthrottled)
    messages: list[str] = []

    mm.ensure("adaface", str(tmp_path / "cache"), log=messages.append)

    percents = [m for m in messages if "%" in m]
    assert percents, f"no progress was reported, only {messages}"
    assert percents[-1].endswith("100% of 0 MB")
    assert all("adaface model" in m for m in percents)
    # Monotonic, and never past 100 -- the last block is short, and urlretrieve
    # reports blocks * block_size, which overshoots if it is not clamped.
    values = [int(m.split(":")[1].strip().split("%")[0]) for m in percents]
    assert values == sorted(values) and values[-1] == 100


def test_progress_is_throttled_rather_than_written_per_block():
    """urlretrieve calls back every 8 KB; 30k GUI writes per model is not progress."""
    messages: list[str] = []
    hook = mm.download_progress(messages.append, "adaface model", 250 * 1024 * 1024)

    for block in range(2000):
        hook(block, 8192, 250 * 1024 * 1024)

    assert len(messages) <= 2, f"{len(messages)} messages for one download burst"


def test_progress_reports_bytes_when_the_server_withholds_a_length():
    """Content-Length is optional; -1 must degrade to MB, not crash or divide by zero."""
    messages: list[str] = []
    hook = mm.download_progress(messages.append, "adaface model", 0, interval=0)

    hook(64, 1024 * 1024, -1)

    assert messages == ["downloading adaface model: 64 MB"]


def test_progress_reporting_is_skipped_entirely_without_a_log():
    """urlretrieve(reporthook=None) is the no-op path, so callers need no branch."""
    assert mm.download_progress(None, "adaface model", 1234) is None


def test_a_corrupt_download_is_refused_and_leaves_nothing_behind(manifest, tmp_path, monkeypatch):
    """A substituted file must never become the model this app then trusts."""
    cache = tmp_path / "cache"
    monkeypatch.setattr(mm.urllib.request, "urlretrieve", _writes(b"x" * len(CONTENT)))

    with pytest.raises(ModelUnavailableError, match="sha256"):
        mm.ensure("adaface", str(cache))
    assert not (cache / "models" / "adaface" / "adaface_ir101_w12m.onnx").exists()
    assert list((cache / "models" / "adaface").glob("*")) == []


def test_a_truncated_download_is_refused(manifest, tmp_path, monkeypatch):
    monkeypatch.setattr(mm.urllib.request, "urlretrieve", _writes(CONTENT[:5]))
    with pytest.raises(ModelUnavailableError, match="expected"):
        mm.ensure("adaface", str(tmp_path / "cache"))


def test_a_truncated_cache_copy_is_not_mistaken_for_the_model(manifest, tmp_path, monkeypatch):
    """An interrupted older download must be replaced, not loaded."""
    cache = tmp_path / "cache"
    partial = cache / "models" / "adaface" / "adaface_ir101_w12m.onnx"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(CONTENT[:3])
    monkeypatch.setattr(mm.urllib.request, "urlretrieve", _writes(CONTENT))

    assert mm.ensure("adaface", str(cache)).read_bytes() == CONTENT


def test_an_unobtainable_model_says_how_to_regenerate_it(manifest, tmp_path, monkeypatch):
    """No copy and no URL: the one case downloading cannot fix.

    ``missing_reason`` is what the detect stage asks *before* fetching buffalo_l
    and YOLOX, so this message is also what a user sees on the status card.
    """
    entry = json.loads(manifest.read_text(encoding="utf-8"))
    del entry["models"][0]["url"]
    manifest.write_text(json.dumps(entry), encoding="utf-8")

    assert not mm.obtainable("adaface", str(tmp_path))
    reason = mm.missing_reason("adaface", str(tmp_path), feature="people detection")
    assert "people detection" in reason
    assert "tools/build/adaface_export.py" in reason
    with pytest.raises(ModelUnavailableError):
        mm.ensure("adaface", str(tmp_path))


def test_an_unreadable_manifest_is_a_user_facing_error(tmp_path, monkeypatch):
    """An app installed outside a checkout has no manifest; that is not a crash."""
    monkeypatch.setattr(mm, "MANIFEST_PATH", tmp_path / "nowhere.json")
    with pytest.raises(ModelUnavailableError, match="ARCHIVE_MODELS_DIR"):
        mm.entry("adaface")
    assert not mm.obtainable("adaface", str(tmp_path))


@pytest.mark.parametrize(
    "mutation",
    [
        {"file": "/etc/passwd"},
        {"file": "../../escape.onnx"},
        {"sha256": "nope"},
        {"size": 0},
        {"url": "http://insecure.invalid/x.onnx"},
    ],
)
def test_a_malformed_entry_is_rejected(manifest, mutation):
    """The schema check packaging relies on now protects the app's cache too."""
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["models"][0].update(mutation)
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        mm.load()


def _forbidden(*_args, **_kwargs):
    raise AssertionError("downloaded a model that was already available locally")


# Bound before any test can monkeypatch the name, so _unthrottled below calls the
# real implementation rather than whatever it is currently standing in for.
_DOWNLOAD_PROGRESS = mm.download_progress


def _unthrottled(log, label, total, **_kwargs):
    """``download_progress`` with the wall-clock throttle disabled.

    A test download finishes in microseconds, so the real one-second interval
    would suppress every update and the assertions would pass vacuously.
    """
    return _DOWNLOAD_PROGRESS(log, label, total, interval=0)


def _writes(payload: bytes, *, blocks: int = 4):
    """Stand in for urlretrieve, driving ``reporthook`` the way the real one does.

    The callback signature is the contract that matters here: block count, block
    size, total size -- with the total reported as the server's Content-Length.
    Splitting the payload into a few blocks is what lets a test see progress at
    all, and it is why the size passed back is the payload's, not the manifest's.
    """

    def urlretrieve(_url, destination, reporthook=None):
        with open(destination, "wb") as handle:
            handle.write(payload)
        if reporthook is not None:
            block_size = max(1, -(-len(payload) // blocks))
            for block in range(blocks + 1):
                reporthook(block, block_size, len(payload))
        return destination, None

    return urlretrieve
