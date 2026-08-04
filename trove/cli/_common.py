"""Helpers shared by more than one command module."""

from __future__ import annotations


def _fmt_bytes(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if f < 1024 or unit == "PB":
            return f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} PB"
