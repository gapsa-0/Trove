"""Entry point for ``python -m organize_archive.cli``.

Needed because ``cli`` is a package: as a single module it ran its own
``if __name__ == "__main__"`` block, but ``python -m`` on a package looks for
this file instead and fails outright without it. That invocation is how the
app is launched in practice, so it has to keep working.
"""

from . import main

raise SystemExit(main())
