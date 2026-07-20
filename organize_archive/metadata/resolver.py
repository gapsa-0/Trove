"""Combine date candidates from all sources into one best datetime.

Each source yields (datetime, confidence). The resolver walks the configured
priority order and takes the first source that produced a value, recording
which source won so the choice stays auditable and tunable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_EXIF_FMTS = ("%Y:%m:%d %H:%M:%S", "%Y:%m:%d %H:%M:%S%z")


def _tz(name: str | None):
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        return None


def to_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def epoch_to_wall(epoch: int, tzname: str | None) -> datetime:
    """Convert a UTC epoch to naive local wall-clock time in the given zone
    (or UTC when no zone configured)."""
    tz = _tz(tzname)
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    if tz is not None:
        dt = dt.astimezone(tz)
    return dt.replace(tzinfo=None)


def exif_datetime(tags: dict) -> tuple[datetime, float] | None:
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


def resolve(candidates: dict[str, tuple[datetime, float]],
            priority: list[str]) -> tuple[datetime, str, float] | None:
    """Pick the first source in `priority` that produced a candidate."""
    for source in priority:
        cand = candidates.get(source)
        if cand is not None:
            dt, conf = cand
            return dt, source, conf
    return None
