"""Combine date candidates from all sources into one best datetime.

Each source yields (datetime, confidence). The resolver walks the configured
priority order and takes the first source that produced a value, recording
which source won so the choice stays auditable and tunable.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_EXIF_FMTS = ("%Y:%m:%d %H:%M:%S", "%Y:%m:%d %H:%M:%S%z")


def _tz(name: str | None) -> ZoneInfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        # Broad on purpose: ZoneInfoNotFoundError (unknown name, a KeyError
        # subclass) and ValueError (malformed key) are expected, but the tzdata
        # lookup can also raise OSError -- not certain that list is exhaustive,
        # so stay broad rather than risk a config value crashing the caller.
        # This is a pure fallback (caller treats None as "no zone"), so silence
        # toward the caller is correct; just note what was rejected.
        logger.debug("unusable timezone %r; falling back to no zone", name, exc_info=True)
        return None


def to_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def epoch_to_wall(epoch: int, tzname: str | None) -> datetime:
    """Convert a UTC epoch to naive local wall-clock time in the given zone
    (or UTC when no zone configured)."""
    tz = _tz(tzname)
    dt = datetime.fromtimestamp(epoch, tz=UTC)
    if tz is not None:
        dt = dt.astimezone(tz)
    return dt.replace(tzinfo=None)


def exif_datetime(tags: dict[str, Any]) -> tuple[datetime, float] | None:
    """Best EXIF datetime from DateTimeOriginal (preferred) or CreateDate."""
    for key, conf in (("DateTimeOriginal", 0.9), ("CreateDate", 0.8)):
        raw = tags.get(key)
        if not raw or str(raw).startswith(("0000", "    ")):
            continue
        s = str(raw)
        for fmt in _EXIF_FMTS:
            try:
                dt = datetime.strptime(s, fmt).replace(tzinfo=None)
                if 1990 <= dt.year <= 2035:
                    return dt, conf
            except ValueError:
                continue
    return None


def resolve(
    candidates: dict[str, tuple[datetime, float]], priority: list[str]
) -> tuple[datetime, str, float] | None:
    """Pick the first source in `priority` that produced a candidate."""
    for source in priority:
        cand = candidates.get(source)
        if cand is not None:
            dt, conf = cand
            return dt, source, conf
    return None
