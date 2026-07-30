# Dependencies

Three files describe what this project depends on, and each answers a different
question:

| File | Question | Mechanism |
| --- | --- | --- |
| `pyproject.toml` | What is **allowed**? | Open `>=` ranges — correct for library metadata, wrong for reproducing a bug |
| `constraints.txt` | What is **tested**? | Exact pins, applied with `pip install -c` |
| `packaging/requirements-desktop.txt` | What **ships**? | The frozen desktop runtime |

`pyproject.toml`'s six optional extras (below) declare the lower bound each
feature needs to function at all — `numpy>=1.24`, not `numpy==2.5.1` — because
that file is also the package's public metadata, read by anyone who installs
`organize-archive` as a library. `constraints.txt` narrows that down to the
exact versions this checkout is developed and tested against, and CI installs
with the same `-c constraints.txt` flag a developer does, so a green run here
and a green run in CI mean the same thing.

A constraint is not an install. Listing `scipy==1.18.0` in `constraints.txt`
pins `scipy` to that version *if* something else pulls it in — scikit-learn
does, for clustering — but `constraints.txt` on its own installs nothing.
Nobody runs `pip install -c constraints.txt` with no other argument; the
extras in `pyproject.toml` decide what gets installed, `constraints.txt`
decides which version of it.

## The core needs nothing

Scanning, indexing and status (`oa scan`, `oa init`, `oa status`) run on the
Python standard library alone, plus two system binaries: `exiftool` and
`ffprobe`. `organize_archive/cli.py`'s `_preflight()` checks for both on PATH
and, in `cmd_init`, prints a note (not an error) if either is missing —
metadata resolution still works, falling back to Takeout sidecars, filename
parsing and mtime. Nothing in `dependencies = []` at the top of
`[project]` in `pyproject.toml` needs to change for the core to work; it is
empty on purpose.

## The six extras

Every optional dependency is *probed*, not assumed. The pattern recurs across
`organize_archive/embeddings/backend.py`, `organize_archive/faces/backend.py`,
`organize_archive/pets/backend.py` and `organize_archive/dedup/exact.py`: an
`available()` function imports the packages inside a `try`, catches broadly
(a half-installed native extension fails in more ways than `ImportError` —
a missing `.so` is `OSError`, a mismatched onnxruntime build is
`RuntimeError`), logs at DEBUG because running without the extra is a
supported configuration, and returns `False`. The feature that depends on it
then reports itself unavailable instead of crashing. `oa faces`, `oa pets` and
semantic indexing all check this before starting a job; the GUI surfaces it as
"unavailable" rather than an error (see `docs/troubleshooting.md`).

| Extra | Packages | Enables | Without it |
| --- | --- | --- | --- |
| `cli` | `rich>=13` | Coloured tables and progress bars | `oa` still runs; output is plain text |
| `media` | `pyexiftool>=0.5`, `Pillow>=10`, `pillow-heif>=0.16`, `ImageHash>=4.3` | Perceptual dedup and HEIC/image decoding | Exact (SHA-256) dedup still works — `hashing/hasher.py` is stdlib `hashlib` only — but `dedup/exact.py`'s `perceptual_available()` returns `False` and cross-format near-duplicates (the same photo re-compressed by a different takeout) go undetected |
| `faces` | `insightface>=1.0`, `onnxruntime>=1.20`, `opencv-python>=4.8`, `scikit-learn>=1.3`, `numpy>=1.24`, `faiss-cpu>=1.13` | Face detection (SCRFD), embedding (AdaFace ir101) and clustering into People | `oa faces` reports the stage unavailable; nothing crashes |
| `pets` | `onnxruntime>=1.20`, `opencv-python>=4.8`, `numpy>=1.24`, `Pillow>=10`, `pillow-heif>=0.16` | YOLOX animal detection plus DINOv2 pet re-identification | `oa pets` reports the stage unavailable |
| `semantic` | `onnxruntime>=1.20`, `tokenizers>=0.20`, `numpy>=1.24`, `Pillow>=10`, `pillow-heif>=0.16` | Local SigLIP 2 search-by-description | Indexing and search both report unavailable — see the gap noted below for one place this degrades less gracefully |
| `dev` | `pytest>=8`, `ruff>=0.6`, `pre-commit>=3` | Running the test suite and linting | You can't develop the project, but a packaged build needs none of it |

`faces` is the extra with the most going on, and its own comment in
`pyproject.toml` is worth reading directly: SCRFD det_10g detects faces
(weights fetched once into the cache dir), the self-exported AdaFace ir101
embeds them, scikit-learn's agglomerative merge stages and faiss's k-NN search
build and expand cluster cores, and `insightface` itself is a pure-Python
wheel supplying only the model-zoo loader and alignment helpers — the
inference runs on onnxruntime, not inside insightface.

`faiss` gets special treatment because it has a real fallback rather than an
on/off switch. `organize_archive/faces/cluster.py`'s `_faiss()` helper tries
to import it and returns `None` on any failure; `_knn_search` then picks
between two implementations: FAISS's `IndexFlatIP` (exact inner product,
tuned BLAS/SIMD) or a blocked-GEMM path over plain NumPy. Both are exact — the
blocked path is not an approximation, it's kept partly as the reference the
tests assert FAISS against — so clustering without faiss produces the same
groupings, just slower on a large archive. This is the one dependency in the
`faces` extra you could plausibly drop on a machine faiss won't build for and
still get correct results.

`pets` and `semantic` share most of their dependency list with `faces`
(`onnxruntime`, `numpy`, `Pillow`, `pillow-heif`) but are kept as separate
extras rather than folded in. The `pets` comment in `pyproject.toml` explains
why: a catalogue that only wants People shouldn't have to download the animal
detector's weights too.

## The `transformers` / `tokenizers` trap

`constraints.txt` pins `tokenizers==0.23.1` — the version the desktop build
ships (`packaging/requirements-desktop.txt` carries the identical pin, with a
comment explaining that `tokenizers` publishes cp310-abi3 wheels, so it needs
no compiler on the release runners even under Python 3.13). `transformers` 5.x
requires `tokenizers<=0.23.0`, one patch below that. Installing `transformers`
on a development machine therefore drags `tokenizers` down a version from what
the shipped app runs.

This is expected, not a bug to fix. `transformers` is a dev-only reference
dependency: it appears in no extra in `pyproject.toml`, no `make` target
installs it, and the packaged app excludes it entirely (`docs/release.md`'s
build-inputs section explains why scikit-learn and SciPy's `array_api_compat`
shims make excluding it from the PyInstaller build worth doing explicitly).
Its only consumer is `tests/unit/test_siglip_preprocessing.py`, which checks the
project's from-scratch SigLIP preprocessing against `transformers`' own
reference tokenizer — the load-bearing test for a class of bug (wrong resize,
wrong normalisation) that costs retrieval quality silently, with every vector
still the right shape. That file guards the import with
`pytest.importorskip("transformers", reason="transformers is a dev-only
reference dependency")` at module level, so a missing `transformers` skips the
whole module at collection time rather than failing.

Concretely: on a machine that has `transformers` installed, the full suite
collects 294 tests and reports `293 passed, 1 xfailed` (the xfail is
unrelated — a pets test superseded by the fused detector, in
`tests/integration/test_pets.py`). Without `transformers`, the eight tests in
`test_siglip_preprocessing.py` collapse into a single collection-time skip:
`285 passed, 1 xfailed, 1 skipped`. Both are green. If you want those eight
preprocessing-parity tests to actually run, install `transformers` by hand and
accept that it holds `tokenizers` back a version — do not add it to any extra
or constraint to make that friction go away.

## A known gap

The graceful-degradation story above is not universal. `semantic_search` in
`organize_archive/services/search.py` does `import numpy as np` partway through
the function body, with no `try`/`except` around it. Every other
optional dependency in this codebase is probed through an `available()`-style
check before it's used; this one isn't. Calling semantic search on an install
that has embeddings already indexed but has since lost the `semantic`/`faces`
extras raises `ModuleNotFoundError` instead of reporting the feature
unavailable.

In practice this is narrow: reaching it needs a catalogue that already has
rows in `semantic_embeddings`, and producing those rows in the first place
requires the `semantic` extra (numpy included) to have been present during
indexing. So the failure mode is specifically installing the extras, indexing
an archive, and then uninstalling them — not a fresh install missing a
dependency. It is known and unfixed. Closing it is a deliberate change to
`semantic_search`'s contract, not a drive-by edit: decide what the function
should return when it cannot score anything, and cover it with a test.

## Bumping a pin

Change the version in `constraints.txt`, run `.venv/bin/python -m pytest -q`,
and — if the pin touches the faces, pets or semantic path — also run
`test_siglip_preprocessing.py` with the models downloaded and `transformers`
installed, since that's the one place that exercises a real dependency's
numerical behaviour rather than a mock of it. Commit the bump on its own, with
the reason, never as a side effect of an unrelated change: `constraints.txt`'s
own header says the same, and it's there because a model backend that
behaves differently under a new `onnxruntime` or `opencv` build is exactly
the class of bug pinning exists to catch, and that's much harder to isolate
in a commit that also changed something else.

Where a pin in `constraints.txt` also appears in
`packaging/requirements-desktop.txt` (currently `Pillow`, `pillow-heif`,
`ImageHash`, `pyexiftool`, `numpy`, `opencv-python`, `onnxruntime`,
`scikit-learn`, `insightface` and `tokenizers`), the two files carry the same
number deliberately, so bump both together.

The two files aren't applied the same way, which matters for anything pinned
in only one. `.github/workflows/ci.yml` installs with `-c constraints.txt`,
same as `make setup`, so every pin there — including the "transitive, but
version-sensitive" ones on `scipy`, `scikit-image` and `onnx` — is enforced
for development and testing. `.github/workflows/release.yml` and
`release-candidate.yml`, which build the desktop package, install with
`pip install -e '.[dev]' -r packaging/requirements-desktop.txt` and no
constraints file at all, so those three float to whatever `scikit-learn` and
`insightface` resolve to at build time; only what's actually listed in
`requirements-desktop.txt` is frozen there. `PyInstaller==6.21.0` is the
reverse case: packaging-only, no `constraints.txt` entry.

## Installing

```
make setup
```

runs, among other things,

```
pip install -e '.[dev,cli,media,faces,pets,semantic]' -c constraints.txt
```

which is also the exact line CI runs, so a green CI run and a green local run
mean the same thing. On a machine without a system `python3.13`, point the
venv creation step at whichever interpreter you do have:

```
make setup PYTHON=/path/to/python3.13
```

`make setup` also runs `pre_commit install` and `npm ci` in `desktop/`; skip
straight to the `pip install` line above if you only want the Python side.
