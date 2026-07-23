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
binaries = []
hiddenimports = []
for package in ("PIL", "PIL.Image", "pillow_heif", "cv2", "onnxruntime", "sklearn", "numpy"):
    hiddenimports += collect_submodules(package)
    binaries += collect_dynamic_libs(package)

a = Analysis([str(root / "packaging" / "desktop_entry.py")], pathex=[str(root)], binaries=binaries,
             datas=datas, hiddenimports=hiddenimports, noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="organize-archive-backend", console=not sys.platform.startswith("win"))
COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="backend")
