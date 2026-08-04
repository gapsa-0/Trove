"""PyInstaller entry point for the packaged desktop backend.

PyInstaller executes its input as a top-level script.  Pointing it directly at
``trove/desktop.py`` therefore breaks that module's relative imports
in the frozen application.  This small absolute-import wrapper keeps the
package context intact.
"""

from trove.desktop import main

if __name__ == "__main__":
    raise SystemExit(main())
