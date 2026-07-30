"""Public surface of the config package.

``config.py`` split into this package (settings / ignore rules / per-archive
registry, each its own job) — this file re-exports the same names so every
existing ``from .config import X`` / ``from ..config import X`` /
``from organize_archive.config import X`` keeps resolving unchanged.
"""

from __future__ import annotations

# Re-export: moved to ignore.py, kept here so call sites importing from the
# package don't need to know about the internal split.
from .ignore import (  # noqa: F401
    IGNORE_EXTENSIONS,
    IGNORE_FILENAMES,
    IGNORE_NAME_SUBSTRINGS,
)

# Re-export: moved to settings.py, kept here so call sites importing from the
# package don't need to know about the internal split.
from .settings import (  # noqa: F401
    DEFAULT_ROOTS,
    PROJECT_ROOT,
    Config,
    discard_superseded_secrets,
)
