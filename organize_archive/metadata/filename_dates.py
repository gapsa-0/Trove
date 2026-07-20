"""Parse a capture date out of a filename.

Many devices/apps encode the date in the name in different formats. Each match
yields a naive datetime (local wall-clock as written) plus a confidence:
a full date+time is more trustworthy than a date alone.
"""

from __future__ import annotations

import re
from datetime import datetime

_MIN_YEAR = 1990
_MAX_YEAR = 2035


def _valid(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if _MIN_YEAR <= dt.year <= _MAX_YEAR else None


def _dt(y, mo, d, h=0, mi=0, s=0) -> datetime | None:
    try:
        return _valid(datetime(int(y), int(mo), int(d), int(h), int(mi), int(s)))
    except ValueError:
        return None


# (regex, builder, confidence). Higher confidence tried first.
# Builders receive the match and return a datetime or None.
_PATTERNS: list[tuple[re.Pattern, callable, float]] = [
    # IMG_20220514_090957 / VID_20220514_090957 / 20220514_090957 / PXL_..._123
    (re.compile(r"(?<!\d)(20\d{2}|19\d{2})(\d{2})(\d{2})[_\-\.T](\d{2})(\d{2})(\d{2})"),
     lambda m: _dt(m[1], m[2], m[3], m[4], m[5], m[6]), 0.9),
    # 2022-05-14 09.09.57 / 2022-05-14_09-09-57 / 2022:05:14 09:09:57
    (re.compile(r"(?<!\d)(20\d{2}|19\d{2})[\-:\.](\d{2})[\-:\.](\d{2})[ _T]"
                r"(\d{2})[\-:\.](\d{2})[\-:\.](\d{2})"),
     lambda m: _dt(m[1], m[2], m[3], m[4], m[5], m[6]), 0.9),
    # WhatsApp: IMG-20220514-WA0001  (date only)
    (re.compile(r"(?<!\d)(20\d{2}|19\d{2})(\d{2})(\d{2})-WA\d+", re.IGNORECASE),
     lambda m: _dt(m[1], m[2], m[3]), 0.6),
    # Compact date+time with no separator: 20220514090957
    (re.compile(r"(?<!\d)(20\d{2}|19\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(?!\d)"),
     lambda m: _dt(m[1], m[2], m[3], m[4], m[5], m[6]), 0.75),
    # Date only: 2022-05-14 or 2022_05_14 or 2022.05.14
    (re.compile(r"(?<!\d)(20\d{2}|19\d{2})[\-_\.](\d{2})[\-_\.](\d{2})(?!\d)"),
     lambda m: _dt(m[1], m[2], m[3]), 0.55),
    # Date only compact: 20220514 (last resort, easily a false positive)
    (re.compile(r"(?<!\d)(20\d{2}|19\d{2})(\d{2})(\d{2})(?!\d)"),
     lambda m: _dt(m[1], m[2], m[3]), 0.4),
]


def parse(name: str) -> tuple[datetime, float] | None:
    """Return (datetime, confidence) for the first matching pattern, else None."""
    for rx, build, conf in _PATTERNS:
        m = rx.search(name)
        if m:
            dt = build(m)
            if dt is not None:
                return dt, conf
    return None
