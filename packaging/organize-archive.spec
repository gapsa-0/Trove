# Build with: python -m PyInstaller packaging/organize-archive.spec
# External tools are supplied under packaging/tools/staged/<target>/ by release CI.
from pathlib import Path
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

# SPECPATH is the directory containing this spec file (``packaging``), so its
# parent is the checkout root.  Using ``parent.parent`` accidentally resolved
# to ``/`` in container/native builds and made PyInstaller look for
# ``/organize_archive/desktop.py``.
root = Path(SPECPATH).parent
datas = collect_data_files("organize_archive")
target = os.environ.get("ARCHIVE_TOOL_TARGET", "")
tools = root / "packaging" / "tools" / "staged" / target
if tools.is_dir():
    datas.append((str(tools), "tools"))
# Model weights with no upstream download URL travel inside the build; the rest
# are fetched once into the cache at first run.  See packaging/models/manifest.json
# and organize_archive.runtime.bundled_model.
models = root / "packaging" / "models" / "staged"
if models.is_dir():
    datas.append((str(models), "models"))
binaries = []
hiddenimports = []
# faiss is in this list for its bundled native library (collect_dynamic_libs):
# the Python package is a thin SWIG wrapper over libfaiss, and without the
# shared object the packaged app imports faiss and then fails at index creation.
# `tokenizers` is here for the same reason as faiss: the Python package is a thin
# wrapper over a Rust extension module, and the app only imports it lazily inside
# the semantic backend, so the native library has to be collected explicitly.
for package in ("PIL", "PIL.Image", "pillow_heif", "cv2", "onnxruntime", "sklearn",
                "numpy", "faiss", "tokenizers"):
    hiddenimports += collect_submodules(package)
    binaries += collect_dynamic_libs(package)
# insightface supplies the buffalo_l model-zoo loader and the face_align helpers.
# Its subpackages are reached through a registry rather than direct imports, so
# PyInstaller cannot see them; its data files carry the model-zoo definitions.
hiddenimports += collect_submodules("insightface.model_zoo")
hiddenimports += collect_submodules("insightface.utils")
datas += collect_data_files("insightface")

# Dev-only weight: torch and transformers exist in a full developer environment
# for tools/dinov2_pet_export.py, and sklearn/scipy reach for torch through their
# array_api_compat shims. The app runs every model on onnxruntime and never
# imports them, but without this the bundle silently grows by ~700 MB whenever
# the build machine happens to have them installed.
#
# Keep this list to packages nothing on the runtime path imports. In particular
# `onnx` and `skimage` look dev-only but are NOT: insightface imports both, and
# excluding them silently disables face detection in the packaged app.
excludes = [
    "torch", "torchvision", "torchaudio", "transformers", "sentence_transformers",
    "matplotlib", "tkinter", "IPython", "pytest",
]

a = Analysis([str(root / "packaging" / "desktop_entry.py")], pathex=[str(root)], binaries=binaries,
             datas=datas, hiddenimports=hiddenimports, excludes=excludes, noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="organize-archive-backend", console=not sys.platform.startswith("win"))
COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="backend")
