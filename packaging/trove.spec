# Build with: python -m PyInstaller packaging/trove.spec
# External tools are supplied under packaging/tools/staged/<target>/ by release CI.
from pathlib import Path
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

# SPECPATH is the directory containing this spec file (``packaging``), so its
# parent is the checkout root.  Using ``parent.parent`` accidentally resolved
# to ``/`` in container/native builds and made PyInstaller look for
# ``/trove/desktop.py``.
root = Path(SPECPATH).parent
# The app's own package data, minus the translator's four large files. Those
# are manifest entries now (trove/translation.py) fetched with Search by
# description, and `collect_data_files` would put them straight back: they still
# sit under trove/web/vendor/ in a source checkout, which is where a developer
# runs from and where `stage-models.py` can seed them.
_FETCHED_VENDOR = ("translate-es-en-", "bergamot-translator-worker.wasm")
datas = [
    entry for entry in collect_data_files("trove")
    if not any(part in Path(entry[0]).name for part in _FETCHED_VENDOR)
]
target = os.environ.get("ARCHIVE_TOOL_TARGET", "")
tools = root / "packaging" / "tools" / "staged" / target
if tools.is_dir():
    datas.append((str(tools), "tools"))
# The manifest itself, which is not a weight but the description of every weight:
# sizes, SHA-256s, and the release-asset URLs the app downloads from. It is the
# one file that must travel even though none of the models do, and it was the one
# file nobody added when they stopped travelling -- `collect_data_files("trove")`
# sweeps the package, and this lives outside it.
#
# The destination is not free-form. `trove/model_manifest.py` resolves the
# manifest relative to the package's own parent, which in a frozen build is the
# bundle root, so it has to land at exactly `packaging/models/` for the same
# expression to answer in a checkout and in a build. Without it every lookup
# raised, and because `present()` is asked "is this weight here" from the
# scheduler's first step, every tick died before a single stage was considered:
# no scan, no indexing, no downloads, and a translator the /vendor route could
# not serve. tests/unit/test_no_bundled_models.py pins the destination against
# the path that module computes.
datas.append((str(root / "packaging" / "models" / "manifest.json"), "packaging/models"))
# No model weights are bundled.  Every one of them -- including the two in
# packaging/models/manifest.json, which have no upstream URL and are therefore
# re-published as release assets on this repository -- is fetched once into the
# cache on first use by trove.model_manifest.  Carrying them here cost
# 349 MB of installer for weights most users download over the same connection
# they would have downloaded them with anyway.  tests/unit/test_no_bundled_models.py
# fails the build if this comes back; see docs/release.md.
binaries = []
hiddenimports = []
# `tokenizers` is in this list for its bundled native library
# (collect_dynamic_libs): the Python package is a thin wrapper over a Rust
# extension module, and the app only imports it lazily inside the semantic
# backend, so the native library has to be collected explicitly.
# `pypdfium2` is here for the same reason: a bundled PDFium shared library,
# imported lazily inside trove/text/pdf.py so PyInstaller never sees it.
# `watchfiles` is the third of the same kind: its `_rust_notify` extension is
# the whole package, and trove/pipeline/watcher.py imports it inside a function
# on purpose (it is optional), so nothing static points at the native module.
for package in ("PIL", "PIL.Image", "pillow_heif", "cv2", "onnxruntime", "sklearn",
                "numpy", "tokenizers", "pypdfium2", "shapely", "pyclipper", "watchfiles"):
    hiddenimports += collect_submodules(package)
    binaries += collect_dynamic_libs(package)
# insightface supplies the buffalo_l model-zoo loader and the face_align helpers.
# Its subpackages are reached through a registry rather than direct imports, so
# PyInstaller cannot see them; its data files carry the model-zoo definitions.
hiddenimports += collect_submodules("insightface.model_zoo")
hiddenimports += collect_submodules("insightface.utils")
datas += collect_data_files("insightface")
# RapidOCR carries its YAML config as package data, and the app cannot start its
# engine without it. It also carries three ONNX weights in the same directory --
# 26.5 MB compressed, the largest single item this bundle used to hold -- which
# are manifest entries now, downloaded on first use like every other weight
# (ADR 0019). Filtering by extension rather than by path because the config and
# the models are siblings under `rapidocr/`, so there is no directory to exclude.
datas += [entry for entry in collect_data_files("rapidocr") if not entry[0].endswith(".onnx")]
hiddenimports += collect_submodules("rapidocr")

# Dev-only weight: torch and transformers exist in a full developer environment
# for tools/build/dinov2_pet_export.py, and sklearn/scipy reach for torch through their
# array_api_compat shims. The app runs every model on onnxruntime and never
# imports them, but without this the bundle silently grows by ~700 MB whenever
# the build machine happens to have them installed.
#
# Keep this list to packages nothing on the runtime path imports. In particular
# `onnx` and `skimage` look dev-only but are NOT: insightface imports both, and
# excluding them silently disables face detection in the packaged app.
#
# faiss is excluded for a different reason: it is genuinely optional, and
# faces/knn.py uses it when present. It is in the `dev` extra -- the tests assert
# the FAISS and NumPy search paths agree -- so every build machine has it, and
# without this line the build would silently pick it up and put 62 MB back,
# 37 MB of which is its own private copy of OpenBLAS.
#
# huggingface_hub and its two dependencies are a third kind: nothing here
# imports them, and they arrive only because `tokenizers` declares them. The
# app loads its tokenizer from a file it fetched itself
# (embeddings/backend.py: `Tokenizer.from_file`), and the SigLIP 2 ONNX files
# come from the same module's own URL constants -- no Hub client is involved on
# any path. They are 3.8 MB of compressed installer, most of it `hf_xet`, a
# Rust extension for an upload protocol this app cannot reach.
#
# The one thing this forbids is `Tokenizer.from_pretrained`, which reaches for
# `hf_hub_download` from inside the Rust extension. If a future change wants
# it, these three come back -- and it would be fetching a tokenizer at runtime
# from a URL nothing pins, which is its own decision to make.
excludes = [
    "torch", "torchvision", "torchaudio", "transformers", "sentence_transformers",
    "matplotlib", "tkinter", "IPython", "pytest",
    "faiss",
    "huggingface_hub", "hf_xet", "fsspec",
]

a = Analysis([str(root / "packaging" / "desktop_entry.py")], pathex=[str(root)], binaries=binaries,
             datas=datas, hiddenimports=hiddenimports, excludes=excludes, noarchive=False)


def _staged_duplicate(entry):
    """A second copy, in the bundle root, of a file already staged under tools/.

    The staged tools are collected as data, but PyInstaller also *scans* what it
    collects, so ffmpeg's own dependencies pull every ``libav*`` into the root a
    second time. On Linux that costs nothing -- two identical files become a
    symlink into ``tools/`` -- but Windows has no symlink to fall back on, so
    0.3.0's installer carried all seven libraries twice: 177 MB raw, about 50 MB
    of the download, ``avcodec-62.dll`` alone being 98 MB of it.

    Nothing loads them from the root. ffmpeg.exe resolves its DLLs from its own
    directory, which is exactly where staging puts them, and on Linux
    ``trove.runtime.tool_env`` prepends that directory to ``LD_LIBRARY_PATH``
    for the same reason -- its docstring explains why the upstream RPATH cannot
    be relied on to do it.
    """
    destination, source, kind = entry
    if Path(destination).parent != Path("."):
        return False  # the staged copy under tools/ itself, which is the one that matters
    if kind == "SYMLINK":
        return Path(source).parts[:1] == ("tools",)
    try:
        return tools.is_dir() and Path(source).resolve().is_relative_to(tools.resolve())
    except OSError:
        return False


a.binaries = [entry for entry in a.binaries if not _staged_duplicate(entry)]
a.datas = [entry for entry in a.datas if not _staged_duplicate(entry)]

# OpenCV's Qt payload must never reach a build. `requirements-desktop.txt` pins
# opencv-python-headless, but insightface and rapidocr both require the full
# wheel, so pip installs it too, both write the same `cv2/`, and whichever landed
# last is what gets frozen -- which is how 0.3.0 shipped Qt5 Core, Gui, Widgets,
# XcbQpa, the xcb/xkb plugins and a second FFmpeg: 36 MB raw, ~13 MB of
# installer, in a program that never opens a window.
# tests/unit/test_opencv_headless.py could not have caught it: it asks whether
# the code calls a GUI function, not which wheel answers `import cv2`.
#
# Named precisely, because `libxcb`/`libxkbcommon` look like evidence and are
# not -- Pillow carries its own copies for its own reasons, and a guard that
# cries wolf on a clean build is a guard the next person deletes.
_QT = ("libqt", "qt5", "qt6", "libqxcb")
_qt_payload = sorted({
    destination for destination, _source, _kind in a.binaries + a.datas
    if Path(destination).name.lower().startswith(_QT)
    or "cv2/qt/" in Path(destination).as_posix()
    # The full wheel's private library directory, whose headless twin is
    # `opencv_python_headless.libs`. This is the one that carries the second
    # libav* set, which is not Qt but arrives for exactly the same reason.
    or Path(destination).as_posix().startswith("opencv_python.libs/")
})
if _qt_payload:
    raise SystemExit(
        "error: this build would ship OpenCV's GUI backend:\n  "
        + "\n  ".join(_qt_payload[:8])
        + f"\n  ... and {len(_qt_payload) - 8} more" * (len(_qt_payload) > 8)
        + "\nThe full opencv-python wheel is installed. Fix the environment with:\n"
        + f"  {sys.executable} packaging/scripts/ensure-headless-opencv.py"
    )

pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="trove-backend", console=not sys.platform.startswith("win"))
COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="backend")
