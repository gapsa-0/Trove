"""The shipped package must not reach for OpenCV's GUI backend.

packaging/requirements-desktop.txt pins ``opencv-python-headless``: the same
OpenCV version and sources as ``opencv-python``, built without highgui. That
choice is worth 29 MB of installer -- the full wheel bundles Qt5
Core/Gui/Widgets/Test/XcbQpa plus the xcb/xkb stack -- and it is safe only for
as long as nothing here calls a window function.

The trap is that headless does not *remove* those names. ``cv2.imshow`` still
exists; it raises at call time with "rebuild the library with GTK+" -- which a
developer with the full wheel installed will never see, and which reaches a user
as a crashed pipeline stage. So check statically, the same way
test_no_console_windows.py does for console flashes.

Note this deliberately does not cover ``VideoCapture``/``VideoWriter``: headless
keeps videoio, so those would work. They are absent for a different reason --
video frames come from the bundled ffmpeg -- and that is not this test's business.

What this file could not see on its own is which *wheel* answers ``import cv2``.
insightface and rapidocr both require the full ``opencv-python``, so pip installs
it beside the headless pin, and 0.3.0 shipped 36 MB of Qt and duplicate FFmpeg
with every call site in this repository still perfectly clean. The build refuses that payload now
(``packaging/trove.spec``) and the build machines undo it first
(``packaging/scripts/ensure-headless-opencv.py``); the last test here is what
keeps that step in the workflows that freeze a build.
"""

from __future__ import annotations

import ast
from pathlib import Path

import trove

PACKAGE = Path(trove.__file__).resolve().parent
ROOT = PACKAGE.parent
WORKFLOWS = ROOT / ".github" / "workflows"
REPAIR = "packaging/scripts/ensure-headless-opencv.py"
FREEZES = ("build:backend", "package:win", "package:linux", "electron-builder")

# highgui entry points. Present as attributes under headless, but every one of
# them throws cv2.error at runtime because the backend was never built.
_GUI_FUNCTIONS = frozenset(
    {
        "imshow",
        "namedWindow",
        "destroyWindow",
        "destroyAllWindows",
        "waitKey",
        "waitKeyEx",
        "pollKey",
        "startWindowThread",
        "moveWindow",
        "resizeWindow",
        "setWindowTitle",
        "setWindowProperty",
        "getWindowProperty",
        "getWindowImageRect",
        "createTrackbar",
        "getTrackbarPos",
        "setTrackbarPos",
        "setTrackbarMin",
        "setTrackbarMax",
        "setMouseCallback",
        "selectROI",
        "selectROIs",
        "imshow_",
    }
)


def _gui_attributes(tree: ast.AST):
    """Yield every ``cv2.<gui function>`` attribute access in a parsed module."""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in _GUI_FUNCTIONS
            and isinstance(node.value, ast.Name)
            and node.value.id == "cv2"
        ):
            yield node


def test_no_module_uses_the_opencv_gui_backend():
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in _gui_attributes(tree):
            rel = path.relative_to(PACKAGE.parent)
            offenders.append(f"  {rel}:{node.lineno}  cv2.{node.attr}")

    assert not offenders, (
        "These calls need OpenCV's highgui backend, which the desktop build does "
        "not ship (packaging/requirements-desktop.txt pins opencv-python-headless).\n"
        "They raise cv2.error for every installed user:\n" + "\n".join(offenders)
    )


def test_the_scanner_would_actually_catch_a_window_call():
    """Guard the guard: a checker that silently matches nothing is worthless."""
    offending = ast.parse("import cv2\ncv2.imshow('w', frame)\ncv2.waitKey(0)\n")
    fine = ast.parse("import cv2\ncv2.cvtColor(a, cv2.COLOR_RGB2BGR)\ncv2.resize(a, (2, 2))\n")

    assert [node.attr for node in _gui_attributes(offending)] == ["imshow", "waitKey"]
    assert list(_gui_attributes(fine)) == []


def _build_workflows() -> list[Path]:
    """Workflows that install the desktop profile, and therefore freeze a build."""
    found = [
        path
        for path in sorted(WORKFLOWS.glob("*.yml"))
        if "requirements-desktop.txt" in path.read_text(encoding="utf-8")
    ]
    assert found, f"no workflow installs the desktop profile; has {WORKFLOWS} moved?"
    return found


def test_every_workflow_that_freezes_a_build_repairs_opencv_first():
    """The step that decides which OpenCV gets frozen, checked by position.

    pip installs the full wheel because insightface and rapidocr ask for it, so
    the repair has to run after every install of the desktop profile and before
    anything freezes -- a step in the right file but the wrong place would be
    exactly as broken as no step at all, and just as green.
    """
    for path in _build_workflows():
        lines = path.read_text(encoding="utf-8").splitlines()
        installs = [n for n, line in enumerate(lines) if "requirements-desktop.txt" in line]
        repairs = [
            n
            for n, line in enumerate(lines)
            if REPAIR in line and line.lstrip().startswith("- run:")
        ]
        freezes = [n for n, line in enumerate(lines) if any(step in line for step in FREEZES)]
        where = path.relative_to(ROOT)

        assert repairs, f"{where} installs the desktop profile but never runs {REPAIR}"
        for install in installs:
            assert any(install < repair for repair in repairs), (
                f"{where}:{install + 1} installs the desktop profile with no {REPAIR} after it"
            )
        if freezes:
            assert min(repairs) < min(freezes), (
                f"{where} freezes a build at line {min(freezes) + 1}, before {REPAIR}"
            )
