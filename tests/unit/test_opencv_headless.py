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
"""

from __future__ import annotations

import ast
from pathlib import Path

import trove

PACKAGE = Path(trove.__file__).resolve().parent

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
