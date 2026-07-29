"""Application-wide log handler installation.

Called from exactly two places -- ``cli.main`` and ``desktop.main`` -- and from
nowhere else. Library modules never configure logging; they only ever do
``logger = logging.getLogger(__name__)`` at module level and log through it.
That split is what keeps a library import from hijacking a host program's
handlers, and it is why every line below lives in one file.

Two handlers are installed on the root logger:

* a rotating file, so a user who reports "it froze" has something to send;
* stderr, because the desktop shell already captures the backend's stderr into
  ``backend-stderr.log`` and feeds it to the "Copy diagnostics" button. That
  feature was written before anything wrote to stderr, so it collected nothing.

Nothing is ever sent off the machine. See ``docs/privacy-and-data.md``.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .paths import app_data_dir

DEFAULT_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

# 5 MB × (1 current + 3 rotated) is a hard ceiling of ~20 MB on disk, chosen so
# the log can never grow into a support problem of its own.
MAX_BYTES = 5_000_000
BACKUP_COUNT = 3

# Libraries that are chatty below WARNING and have nothing to say about this
# application's behaviour. PIL in particular logs every TIFF tag it reads at
# DEBUG, which buries our own lines when OA_LOG_LEVEL=DEBUG is set to debug a
# scan -- exactly when the log matters most.
NOISY_LIBRARIES = ("PIL", "urllib3", "onnxruntime", "matplotlib", "faiss")

# Marks the handlers this module installed, so a second configure() call can
# remove its own without touching anything pytest or a host program added.
# A plain "already configured?" boolean would be worse: it would make a second
# call with a different level silently do nothing.
_OURS = "_organize_archive_handler"


def log_file() -> Path:
    """Path of the current log file. Also what ``oa logs --path`` prints."""
    return app_data_dir() / "logs" / "trove.log"


def configure(level: str | None = None, *, stderr: bool = True) -> None:
    """Install the app's log handlers. Idempotent; safe to call twice.

    ``level`` wins over the ``OA_LOG_LEVEL`` environment variable, which in turn
    wins over ``INFO``. An unrecognised name falls back to ``INFO`` rather than
    raising -- a typo in an env var must not stop the application starting.
    """
    resolved = (level or os.environ.get("OA_LOG_LEVEL") or DEFAULT_LEVEL).strip().upper()
    numeric = logging.getLevelNamesMapping().get(resolved, logging.INFO)

    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, _OURS, False)]:
        root.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(LOG_FORMAT)

    path = log_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
    except OSError as exc:
        # A read-only or unwritable data dir must degrade to stderr-only, not
        # prevent the app from running. Written straight to stderr -- the stream
        # the desktop shell captures -- because no file handler exists to record
        # it and this runs before any logger has somewhere to write.
        sys.stderr.write(f"could not open log file {path}: {exc}\n")
    else:
        file_handler.setFormatter(formatter)
        setattr(file_handler, _OURS, True)
        root.addHandler(file_handler)

    if stderr:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        setattr(stream_handler, _OURS, True)
        root.addHandler(stream_handler)

    root.setLevel(numeric)
    for name in NOISY_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)
