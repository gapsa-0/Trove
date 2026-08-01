"""Google Takeout sidecar matching and parsing.

Takeout writes a JSON sidecar per media file, but the naming is inconsistent:
  IMG.jpg              -> IMG.jpg.json
                       -> IMG.jpg.supplemental-metadata.json   (newer)
  IMG(1).jpg           -> IMG.jpg(1).json                      (counter shifts)
  IMG-edited.jpg       -> IMG.jpg.json                         (reuses original)
  very-long-name.jpg   -> very-long-name.jp.json               (truncated)

The matcher tries these rules in confidence order and, as a last resort, a
truncated-prefix match against the JSON files actually present in the folder.
It never guesses silently: each match records the rule and a confidence, and
non-matches are reported by the caller.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Localised "-edited" suffixes Google appends to edited copies (name stem).
EDITED_SUFFIXES = (
    "-edited",
    "-editado",
    "-ha editado",
    "-bearbeitet",
    "-modifié",
    "-modifiée",
    "-bewerkt",
    "-editat",
)

_COUNTER_RE = re.compile(r"^(.*?)\((\d+)\)(\.[^.]+)?$")
_SUPPL_RE = re.compile(r"\.supplemental-met.*$", re.IGNORECASE)
_TRAIL_COUNTER_RE = re.compile(r"\(\d+\)$")


@dataclass
class SidecarData:
    """Fields parsed out of one Takeout JSON sidecar. ``lat``/``lon``/``alt`` are
    all None together when the sidecar's geo fields were the 0/0/0 "no location" placeholder."""

    title: str | None
    description: str | None
    taken_time: int | None  # photoTakenTime epoch (UTC)
    lat: float | None
    lon: float | None
    alt: float | None


def _candidate_names(name: str) -> list[tuple[str, str, float]]:
    """Ordered (json_filename, method, confidence) candidates for a media name."""
    out: list[tuple[str, str, float]] = [
        (f"{name}.json", "exact", 1.0),
        (f"{name}.supplemental-metadata.json", "supplemental", 1.0),
    ]

    m = _COUNTER_RE.match(name)
    if m:
        stem, num, ext = m.group(1), m.group(2), m.group(3) or ""
        out.append((f"{stem}{ext}({num}).json", "counter", 0.95))
        out.append((f"{stem}{ext}.supplemental-metadata({num}).json", "counter-suppl", 0.95))

    stem_noext, dot, ext = name.rpartition(".")
    base = stem_noext if dot else name
    low = base.lower()
    for suf in EDITED_SUFFIXES:
        if low.endswith(suf):
            orig = base[: -len(suf)] + (f".{ext}" if dot else "")
            out.append((f"{orig}.json", "edited", 0.8))
            out.append((f"{orig}.supplemental-metadata.json", "edited-suppl", 0.8))
            break
    return out


class SidecarMatcher:
    """Finds a media file's Takeout JSON, caching one directory's listing."""

    def __init__(self) -> None:
        self._dir: Path | None = None
        self._jsons: dict[str, Path] = {}

    def _load_dir(self, d: Path) -> None:
        if self._dir == d:
            return
        self._dir = d
        self._jsons = {}
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.name.lower().endswith(".json") and e.is_file(follow_symlinks=False):
                        self._jsons[e.name] = Path(e.path)
        except OSError:
            pass

    @staticmethod
    def _json_base(json_name: str) -> str:
        base = json_name[:-5] if json_name.lower().endswith(".json") else json_name
        base = _SUPPL_RE.sub("", base)
        base = _TRAIL_COUNTER_RE.sub("", base)
        return base

    def _prefix_match(self, name: str) -> tuple[Path, str, float] | None:
        """Truncated-name fallback: a JSON whose base is a prefix of the media
        name, unique in the folder. Guards against short/ambiguous prefixes."""
        matches = []
        for jn in self._jsons:
            b = self._json_base(jn)
            if b and name.startswith(b) and len(b) >= min(20, len(name)):
                matches.append(jn)
        if len(matches) == 1:
            return self._jsons[matches[0]], "prefix-trunc", 0.7
        return None

    def find(self, media_path: Path) -> tuple[Path, str, float] | None:
        """Return (json_path, method, confidence) or None."""
        self._load_dir(media_path.parent)
        if not self._jsons:
            return None
        name = media_path.name
        for jname, method, conf in _candidate_names(name):
            hit = self._jsons.get(jname)
            if hit is not None:
                return hit, method, conf
        return self._prefix_match(name)


def _clean_coord(lat: Any, lon: Any, alt: Any) -> tuple[float | None, float | None, float | None]:
    # lat/lon/alt come straight out of parsed Takeout JSON, so their runtime type
    # is whatever the sidecar happened to contain (float, int, str, null, ...);
    # Any is the honest type here, and num() below is exactly the guard for it.
    def num(x: Any) -> float | None:
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    lat, lon, alt = num(lat), num(lon), num(alt)
    # 0/0 is Takeout's "no location", not the Gulf of Guinea.
    if (not lat) and (not lon):
        return None, None, None
    return lat, lon, alt


def parse_sidecar(json_path: Path) -> SidecarData | None:
    """Parse one Takeout JSON sidecar. Returns None if it can't be read or isn't valid JSON."""
    try:
        data = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None

    taken = None
    pt = (data.get("photoTakenTime") or {}).get("timestamp")
    if pt:
        try:
            taken = int(pt)
        except (TypeError, ValueError):
            taken = None

    geo = data.get("geoData") or {}
    lat, lon, alt = _clean_coord(geo.get("latitude"), geo.get("longitude"), geo.get("altitude"))
    if lat is None:
        ge = data.get("geoDataExif") or {}
        lat, lon, alt = _clean_coord(ge.get("latitude"), ge.get("longitude"), ge.get("altitude"))

    desc = data.get("description") or None
    return SidecarData(
        title=data.get("title"),
        description=desc,
        taken_time=taken,
        lat=lat,
        lon=lon,
        alt=alt,
    )
