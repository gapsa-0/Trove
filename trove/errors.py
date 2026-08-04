"""The errors this application raises deliberately.

The point of the base class is to separate two things that used to arrive at a
caller looking identical: "the user needs to install or download something" and
"this is our bug". An anonymous ``Exception`` cannot be told apart, so calling
code had no way to choose between reporting a fixable condition and logging a
traceback. With a base class the HTTP layer can map ``TroveError`` to a clean
4xx with the message shown as-is, and anything else to a 500 plus a logged
traceback.

Use these at the boundaries -- model loading, archive opening, config reading,
tool discovery -- not for internal errors. A ``ValueError`` inside a parser
should stay a ``ValueError``.

Deliberately NOT subclasses of ``RuntimeError``/``OSError``: nothing in this
codebase catches those by type (checked), so the plain hierarchy is honest.
"""

from __future__ import annotations


class TroveError(Exception):
    """Base for every error this application raises deliberately."""


class ConfigError(TroveError):
    """Unusable configuration: malformed config.json, unwritable data dir."""


class MissingToolError(TroveError):
    """A required external binary (exiftool, ffprobe, ffmpeg) is not installed.

    Note most tool use degrades instead of raising -- ``runtime.tool()`` returns
    None and the caller carries on without that feature, which is the documented
    behaviour. This is for the paths where the tool is genuinely required.
    """


class ModelUnavailableError(TroveError):
    """An ML model, or the runtime that loads it, is missing or unloadable.

    Covers both halves of the same user-facing problem: an optional extra that
    was never installed, and a model file that is absent or truncated. Either
    way the answer is an install or a download, never a bug report.
    """


class ArchiveError(TroveError):
    """An archive cannot be used: root missing, database unreadable, wrong identity."""
