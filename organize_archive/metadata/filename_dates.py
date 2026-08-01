"""Parse a capture date out of a filename.

Patterns are derived from an analysis of the real archive (Android/iOS cameras,
WhatsApp mobile + desktop, Windows Phone, Google Photos bursts, Facebook and
photo-app exports). Each match yields a naive datetime (local wall-clock as
written, or UTC for unix-epoch names) plus a confidence: a full date+time is
more trustworthy than a date alone or a recovered epoch.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime

_MIN_YEAR = 1990
_MAX_YEAR = 2035

# Plausible range for a 13-digit unix-millisecond timestamp (2001-09 .. 2033-05).
_MS_MIN = 1_000_000_000_000
_MS_MAX = 1_999_999_999_999


def _valid(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if _MIN_YEAR <= dt.year <= _MAX_YEAR else None


def _dt(
    y: str | int,
    mo: str | int,
    d: str | int,
    h: str | int = 0,
    mi: str | int = 0,
    s: str | int = 0,
) -> datetime | None:
    try:
        return _valid(datetime(int(y), int(mo), int(d), int(h), int(mi), int(s)))
    except ValueError:
        return None


def _from_ms(ms: str | int) -> datetime | None:
    try:
        v = int(ms)
    except (TypeError, ValueError):
        return None
    if not (_MS_MIN <= v <= _MS_MAX):
        return None
    try:
        return _valid(datetime.fromtimestamp(v / 1000, tz=UTC).replace(tzinfo=None))
    except (ValueError, OSError, OverflowError):
        return None


def _dt_yfirst_rescue(
    y: str | int,
    g1: str | int,
    g2: str | int,
    h: str | int = 0,
    mi: str | int = 0,
    s: str | int = 0,
) -> datetime | None:
    """Year-first date: assume (month=g1, day=g2) first (ISO order); if
    that's invalid, retry as (month=g2, day=g1). Handles the rare Y-D-M
    naming quirk without affecting any name that already parses under the
    standard reading."""
    return _dt(y, g1, g2, h, mi, s) or _dt(y, g2, g1, h, mi, s)


def _forced_day_month(a: int, b: int) -> tuple[int, int] | None:
    """(day, month) if a value > 12 forces the order, else None (ambiguous
    when both <= 12, invalid when both > 12)."""
    if a > 12 and b <= 12:
        return a, b
    if b > 12 and a <= 12:
        return b, a
    return None


def _dt_two_number_date(
    a: str | int,
    b: str | int,
    y: str | int,
    day_first: bool,
    h: str | int = 0,
    mi: str | int = 0,
    s: str | int = 0,
    base_conf: float = 0.55,
) -> tuple[datetime, float] | None:
    """Non-year-led numeric date (DD-MM-YYYY / MM-DD-YYYY). Resolved
    unambiguously when one value is > 12; otherwise falls back to
    `day_first` at a lower confidence since the order is a guess."""
    a, b = int(a), int(b)
    forced = _forced_day_month(a, b)
    if forced is not None:
        day, month = forced
        conf = base_conf
    elif a <= 12 and b <= 12:
        day, month = (a, b) if day_first else (b, a)
        conf = base_conf - 0.10
    else:
        return None  # both > 12: invalid
    dt = _dt(y, month, day, h, mi, s)
    return (dt, conf) if dt is not None else None


# (regex, builder, confidence). Tried in order; first successful build wins.
# Higher-confidence / more-specific patterns come first.
#
# Two builder shapes share this table, discriminated by `conf`: a fixed-confidence
# entry's builder takes just the match and returns a datetime, while a dynamic-
# confidence entry (conf=None, see parse()) takes the match plus `day_first` and
# returns its own (datetime, confidence) pair. A plain Callable can't express an
# arity that varies with a sibling tuple field, so the element type is Any here —
# parse() is where the two shapes actually get told apart.
_PatternBuilder = Callable[..., datetime | tuple[datetime, float] | None]
_PATTERNS: list[tuple[re.Pattern[str], _PatternBuilder, float | None]] = [
    # WhatsApp desktop: "... 2022-05-14 at 09.09.57.jpeg"
    (
        re.compile(
            r"(?<!\d)(20\d{2}|19\d{2})[-.](\d{2})[-.](\d{2})\s+at\s+"
            r"(\d{2})[.:](\d{2})[.:](\d{2})",
            re.IGNORECASE,
        ),
        lambda m: _dt(m[1], m[2], m[3], m[4], m[5], m[6]),
        0.9,
    ),
    # Windows Phone: WP_20180312_12_11_18_Pro  (underscore-separated time)
    (
        re.compile(r"(?<!\d)(20\d{2}|19\d{2})(\d{2})(\d{2})_(\d{2})_(\d{2})_(\d{2})"),
        lambda m: _dt(m[1], m[2], m[3], m[4], m[5], m[6]),
        0.9,
    ),
    # IMG_20220514_090957 / VID_20220514_090957 / 20220514-090957 / with ms suffix
    (
        re.compile(r"(?<!\d)(20\d{2}|19\d{2})(\d{2})(\d{2})[_\-\.T](\d{2})(\d{2})(\d{2})"),
        lambda m: _dt(m[1], m[2], m[3], m[4], m[5], m[6]),
        0.9,
    ),
    # 2022-05-14 09.09.57 / 2022-05-14_09-09-57 / 2022:05:14 09:09:57
    (
        re.compile(
            r"(?<!\d)(20\d{2}|19\d{2})[\-:\.](\d{2})[\-:\.](\d{2})[ _T]"
            r"(\d{2})[\-:\.](\d{2})[\-:\.](\d{2})"
        ),
        lambda m: _dt(m[1], m[2], m[3], m[4], m[5], m[6]),
        0.9,
    ),
    # Compact date+time, optionally with trailing milliseconds:
    # 20220514090957 / BURST20240213093218240 / ..._20201226073741907
    (
        re.compile(r"(?<!\d)(20\d{2}|19\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})\d{0,3}(?!\d)"),
        lambda m: _dt(m[1], m[2], m[3], m[4], m[5], m[6]),
        0.75,
    ),
    # Year-first, fully dash-separated date+time: 2018-14-03-21-07-07.
    # Standard Y-M-D is tried first; rescued as Y-D-M only if that's invalid
    # (no known app writes this shape, so both orders are attempted).
    (
        re.compile(r"(?<!\d)(20\d{2}|19\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})(?!\d)"),
        lambda m: _dt_yfirst_rescue(m[1], m[2], m[3], m[4], m[5], m[6]),
        0.75,
    ),
    # Date + HHMM (4-digit time, no seconds): IMG00243-20120105-1855
    (
        re.compile(r"(?<!\d)(20\d{2}|19\d{2})(\d{2})(\d{2})[-_](\d{2})(\d{2})(?!\d)"),
        lambda m: _dt(m[1], m[2], m[3], m[4], m[5]),
        0.65,
    ),
    # Non-year-led date+time: DD-MM-YYYY_HH-MM / MM-DD-YYYY_HH-MM (e.g. a
    # WhatsApp-forwarded video renamed by the OS locale's date format).
    # Dynamic confidence (see parse()): conf=None marks this entry.
    (
        re.compile(
            r"(?<!\d)(\d{1,2})[-_.](\d{1,2})[-_.](20\d{2}|19\d{2})[_\-](\d{2})[-_.:](\d{2})(?!\d)"
        ),
        lambda m, day_first: _dt_two_number_date(
            m[1], m[2], m[3], day_first, m[4], m[5], base_conf=0.65
        ),
        None,
    ),
    # WhatsApp mobile: IMG-20220514-WA0001  (date only)
    (
        re.compile(r"(?<!\d)(20\d{2}|19\d{2})(\d{2})(\d{2})-WA\d+", re.IGNORECASE),
        lambda m: _dt(m[1], m[2], m[3]),
        0.6,
    ),
    # App exports with a unix-ms timestamp and a known prefix:
    # FB_IMG_1488863651591 / FaceApp_1493811250511 / picture_1628697487196
    (
        re.compile(
            r"(?:FB_IMG|FaceApp|picture|Signal|PANO|IMG|VID)[ _\-]?(1[0-9]{12})(?!\d)",
            re.IGNORECASE,
        ),
        lambda m: _from_ms(m[1]),
        0.6,
    ),
    # Date only: 2022-05-14 / 2022_05_14 / 2022.05.14 (rescued as Y-D-M if
    # the standard Y-M-D reading is invalid, e.g. "2019-25-06").
    (
        re.compile(r"(?<!\d)(20\d{2}|19\d{2})[\-_\.](\d{2})[\-_\.](\d{2})(?!\d)"),
        lambda m: _dt_yfirst_rescue(m[1], m[2], m[3]),
        0.55,
    ),
    # Non-year-led date only: DD-MM-YYYY / MM-DD-YYYY (dynamic confidence).
    (
        re.compile(r"(?<!\d)(\d{1,2})[-_.](\d{1,2})[-_.](20\d{2}|19\d{2})(?!\d)"),
        lambda m, day_first: _dt_two_number_date(m[1], m[2], m[3], day_first, base_conf=0.55),
        None,
    ),
    # Bare 13-digit unix-ms timestamp (low confidence — could be an id).
    (re.compile(r"(?<!\d)(1[0-9]{12})(?!\d)"), lambda m: _from_ms(m[1]), 0.4),
    # Date only compact: 20220514 (last resort, easily a false positive).
    (
        re.compile(r"(?<!\d)(20\d{2}|19\d{2})(\d{2})(\d{2})(?!\d)"),
        lambda m: _dt(m[1], m[2], m[3]),
        0.4,
    ),
]


def parse(name: str, day_first: bool = True) -> tuple[datetime, float] | None:
    """Return (datetime, confidence) for the first matching pattern, else None.

    `day_first` only affects the non-year-led two-number-date patterns
    (DD-MM-YYYY vs MM-DD-YYYY) when neither number forces the order.
    """
    for rx, build, conf in _PATTERNS:
        m = rx.search(name)
        if not m:
            continue
        if conf is None:
            result = build(m, day_first)
            # A dynamic-confidence entry's builder always returns a (datetime,
            # confidence) pair or None -- the isinstance just tells mypy what the
            # `conf is None` branch already guarantees at runtime.
            if isinstance(result, tuple):
                return result
        else:
            dt = build(m)
            if isinstance(dt, datetime):
                return dt, conf
    return None
