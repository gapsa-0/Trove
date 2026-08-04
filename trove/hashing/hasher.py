"""Content hashing: a cheap partial prefilter plus full SHA-256.

Read-only: these functions only open files for reading.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_CHUNK = 1024 * 1024  # 1 MiB streaming chunks


def fast_hash(path: Path, size: int, sample_bytes: int = 65536) -> str:
    """Cheap fingerprint: size + head sample + tail sample.

    Used as a prefilter so we only compute full SHA-256 for files that could
    plausibly be identical. Not collision-proof; never used as ground truth.
    """
    h = hashlib.sha256()
    h.update(str(size).encode())
    with path.open("rb") as f:
        head = f.read(sample_bytes)
        h.update(head)
        if size > sample_bytes * 2:
            f.seek(-sample_bytes, 2)
            h.update(f.read(sample_bytes))
    return h.hexdigest()


def sha256(path: Path) -> str:
    """Full SHA-256 of the file's content, streamed."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()
