"""Parse a capture date out of a filename.

Patterns are derived from an analysis of the real archive (Android/iOS cameras,
WhatsApp mobile + desktop, Windows Phone, Google Photos bursts, Facebook and
photo-app exports). Each match yields a naive datetime (local wall-clock as
written, or UTC for unix-epoch names) plus a confidence: a full date+time is
more trustworthy than a date alone or a recovered epoch.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

_MIN_YEAR = 1990
_MAX_YEAR = 2035

# Plausible range for a 13-digit unix-millisecond timestamp (2001-09 .. 2033-05).
_MS_MIN = 1_000_000_000_000
_MS_MAX = 1_999_999_999_999


def _valid(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if _MIN_YEAR <= dt.year <= _MAX_YEAR else None


def _dt(y, mo, d, h=0, mi=0, s=0) -> datetime | None:
    try:
        return _valid(datetime(int(y), int(mo), int(d), int(h), int(mi), int(s)))
    except ValueError:
        return None


def _from_ms(ms) -> datetime | None:
    try:
        v = int(ms)
    except (TypeError, ValueError):
        return None
    if not (_MS_MIN <= v <= _MS_MAX):
        return None
    try:
        return _valid(
            datetime.fromtimestamp(v / 1000, tz=timezone.utc).replace(tzinfo=None)
        )
    except (ValueError, OSError, OverflowError):
        return None


# (regex, builder, confidence). Tried in order; first successful build wins.
# Higher-confidence / more-specific patterns come first.
_PATTERNS: list[tuple[re.Pattern, "callable", float]] = [
    # WhatsApp desktop: "... 2022-05-14 at 09.09.57.jpeg"
    (re.compile(r"(?<!\d)(20\d{2}|19\d{2})[-.](\d{2})[-.](\d{2})\s+at\s+"
                r"(\d{2})[.:](\d{2})[.:](\d{2})", re.IGNORECASE),
     lambda m: _dt(m[1], m[2], m[3], m[4], m[5], m[6]), 0.9),
    # Windows Phone: WP_20180312_12_11_18_Pro  (underscore-separated time)
    (re.compile(r"(?<!\d)(20\d{2}|19\d{2})(\d{2})(\d{2})_(\d{2})_(\d{2})_(\d{2})"),
     lambda m: _dt(m[1], m[2], m[3], m[4], m[5], m[6]), 0.9),
    # IMG_20220514_090957 / VID_20220514_090957 / 20220514-090957 / with ms suffix
    (re.compile(r"(?<!\d)(20\d{2}|19\d{2})(\d{2})(\d{2})[_\-\.T](\d{2})(\d{2})(\d{2})"),
     lambda m: _dt(m[1], m[2], m[3], m[4], m[5], m[6]), 0.9),
    # 2022-05-14 09.09.57 / 2022-05-14_09-09-57 / 2022:05:14 09:09:57
    (re.compile(r"(?<!\d)(20\d{2}|19\d{2})[\-:\.](\d{2})[\-:\.](\d{2})[ _T]"
                r"(\d{2})[\-:\.](\d{2})[\-:\.](\d{2})"),
     lambda m: _dt(m[1], m[2], m[3], m[4], m[5], m[6]), 0.9),
    # Compact date+time, optionally with trailing milliseconds:
    # 20220514090957 / BURST20240213093218240 / ..._20201226073741907
    (re.compile(r"(?<!\d)(20\d{2}|19\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})\d{0,3}(?!\d)"),
     lambda m: _dt(m[1], m[2], m[3], m[4], m[5], m[6]), 0.75),
    # Date + HHMM (4-digit time, no seconds): IMG00243-20120105-1855
    (re.compile(r"(?<!\d)(20\d{2}|19\d{2})(\d{2})(\d{2})[-_](\d{2})(\d{2})(?!\d)"),
     lambda m: _dt(m[1], m[2], m[3], m[4], m[5]), 0.65),
    # WhatsApp mobile: IMG-20220514-WA0001  (date only)
    (re.compile(r"(?<!\d)(20\d{2}|19\d{2})(\d{2})(\d{2})-WA\d+", re.IGNORECASE),
     lambda m: _dt(m[1], m[2], m[3]), 0.6),
    # App exports with a unix-ms timestamp and a known prefix:
    # FB_IMG_1488863651591 / FaceApp_1493811250511 / picture_1628697487196
    (re.compile(r"(?:FB_IMG|FaceApp|picture|Signal|PANO|IMG|VID)[ _\-]?(1[0-9]{12})(?!\d)",
                re.IGNORECASE),
     lambda m: _from_ms(m[1]), 0.6),
    # Date only: 2022-05-14 / 2022_05_14 / 2022.05.14
    (re.compile(r"(?<!\d)(20\d{2}|19\d{2})[\-_\.](\d{2})[\-_\.](\d{2})(?!\d)"),
     lambda m: _dt(m[1], m[2], m[3]), 0.55),
    # Bare 13-digit unix-ms timestamp (low confidence — could be an id).
    (re.compile(r"(?<!\d)(1[0-9]{12})(?!\d)"),
     lambda m: _from_ms(m[1]), 0.4),
    # Date only compact: 20220514 (last resort, easily a false positive).
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
