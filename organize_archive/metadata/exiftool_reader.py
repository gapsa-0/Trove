"""Read embedded metadata with the exiftool binary (read-only).

exiftool has a slow (~0.2s) Perl startup, so files are processed in large
batches passed via an argfile — startup cost is amortised to nothing and we
avoid command-line length limits. Requesting only the tags we need also makes
exiftool considerably faster.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, cast

from ..runtime import no_window, tool

# Only these tags are requested (order-insensitive). -n forces numeric output
# (decimal GPS, numeric orientation, duration in seconds).
_TAGS = [
    "-FileType",
    "-MIMEType",
    "-ImageWidth",
    "-ImageHeight",
    "-Duration",
    "-Make",
    "-Model",
    "-Orientation",
    "-DateTimeOriginal",
    "-CreateDate",
    "-GPSLatitude",
    "-GPSLongitude",
    "-GPSAltitude",
    "-GPSLatitudeRef",
    "-GPSLongitudeRef",
]


def available() -> bool:
    """Whether the exiftool binary was found on PATH."""
    return tool("exiftool") is not None


class ExifReader:
    """Batch reader for the fixed tag set in ``_TAGS``, backed by one exiftool subprocess per batch."""

    def __init__(self) -> None:
        if not available():
            raise RuntimeError("exiftool not found on PATH")

    def read_batch(self, paths: list[Path]) -> dict[str, dict[str, Any]]:
        """Return {absolute_path_str: {tag: value}} for the given files.

        Files exiftool cannot read are simply absent from the result.
        """
        if not paths:
            return {}

        with tempfile.NamedTemporaryFile("w", suffix=".args", encoding="utf-8", delete=False) as af:
            for p in paths:
                af.write(str(p) + "\n")
            argfile = af.name

        try:
            cmd = [
                # tool() is `str | None` in general, but ExifReader.__init__ already
                # refused to construct unless available() (itself a tool() lookup)
                # returned truthy -- so exiftool was on PATH at construction time.
                # This is a fresh lookup, not the cached result, so the cast is
                # trusting that PATH hasn't changed since construction.
                cast(str, tool("exiftool")),
                "-json",
                "-n",
                "-q",
                "-charset",
                "filename=utf8",
                *_TAGS,
                "-@",
                argfile,
            ]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                errors="replace",
                **no_window(),
            )
        finally:
            Path(argfile).unlink(missing_ok=True)

        out = proc.stdout.strip()
        if not out:
            return {}
        try:
            records = json.loads(out)
        except json.JSONDecodeError:
            return {}

        result: dict[str, dict] = {}
        for rec in records:
            src = rec.pop("SourceFile", None)
            if src is not None:
                result[str(Path(src))] = rec
        return result
