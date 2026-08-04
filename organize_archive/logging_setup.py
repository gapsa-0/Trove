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
from io import TextIOWrapper
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


class _LazyRotatingFileHandler(RotatingFileHandler):
    """A rotating file that touches the disk only when something is logged.

    Two reasons this is not a plain ``RotatingFileHandler``:

    *Lazy directory creation.* The application data directory is created by
    ``Config.ensure_dirs()``, deliberately only by operations that write --
    a command that only reads, or that fails its argument checks, leaves no
    trace. Creating ``logs/`` eagerly in ``configure()`` broke that: every
    ``oa`` invocation materialised the data dir, and ``oa migrate-data`` then
    could not tell a fresh target from an occupied one.

    *Survivable failure.* If the data dir is unwritable (read-only volume,
    permissions) the default behaviour is a traceback on stderr for every
    record logged for the rest of the process's life. This reports once and
    then gets out of the way; the stderr handler carries on alone.
    """

    _broken = False

    # TextIOWrapper, not TextIO: logging.FileHandler declares the concrete
    # type and an override may not widen it.
    def _open(self) -> TextIOWrapper:
        Path(self.baseFilename).parent.mkdir(parents=True, exist_ok=True)
        return super()._open()

    def emit(self, record: logging.LogRecord) -> None:
        if self._broken:
            return
        super().emit(record)

    def handleError(self, record: logging.LogRecord) -> None:
        self._broken = True
        # Not a log call: the logging machinery is what just failed.
        sys.stderr.write(f"could not write log file {self.baseFilename}; continuing without it\n")


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

    # delay=True: no file, and no logs/ directory, until the first record.
    file_handler = _LazyRotatingFileHandler(
        log_file(), maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8", delay=True
    )
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
