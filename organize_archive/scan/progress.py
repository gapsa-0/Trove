"""A dependency-free progress renderer for scans.

On a TTY it draws an in-place bar with percent, throughput and ETA. When output
is redirected (e.g. a background log), it prints an occasional status line
instead so logs stay readable.
"""

from __future__ import annotations

import shutil
import sys
import time


def _fmt_hms(seconds: float) -> str:
    s = int(max(seconds, 0))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{sec:02d}"


def _fmt_gb(nbytes: int) -> str:
    return f"{nbytes / 1e9:.1f} GB"


class ScanProgress:
    def __init__(self, total: int | None, stream=None, width: int = 32):
        self.total = total or 0
        self.stream = stream or sys.stdout
        self.width = width
        self.tty = self.stream.isatty()
        # Redraw often on a terminal; log sparsely when redirected.
        self.min_interval = 0.2 if self.tty else 10.0
        self.start = time.monotonic()
        self.last_draw = 0.0
        self.done = 0
        self.bytes_hashed = 0

    def update(self, done: int, bytes_hashed: int) -> None:
        self.done = done
        self.bytes_hashed = bytes_hashed
        now = time.monotonic()
        final = self.total and done >= self.total
        if now - self.last_draw < self.min_interval and not final:
            return
        self.last_draw = now
        self._render(now)

    def _render(self, now: float) -> None:
        elapsed = now - self.start
        rate = self.done / elapsed if elapsed > 0 else 0.0
        if self.total:
            frac = min(self.done / self.total, 1.0)
            eta = (self.total - self.done) / rate if rate > 0 else 0.0
            pct = f"{frac * 100:5.1f}%"
            counts = f"{self.done}/{self.total}"
            eta_s = f"ETA {_fmt_hms(eta)}"
        else:
            frac = 0.0
            pct = "  --%"
            counts = f"{self.done}"
            eta_s = f"elapsed {_fmt_hms(elapsed)}"

        tail = f"{pct}  {counts}  {_fmt_gb(self.bytes_hashed)}  {rate:.0f} f/s  {eta_s}"
        if self.tty:
            cols = shutil.get_terminal_size((80, 20)).columns
            # Reserve room so the bar always fits on one line (no wrapping),
            # shrinking the bar itself on narrow terminals.
            bar_width = max(4, min(self.width, cols - len(tail) - 4))
            filled = int(bar_width * frac)
            bar = "█" * filled + "░" * (bar_width - filled)
            line = f"[{bar}] {tail}"[: cols - 1]
            # \r returns to column 0; \x1b[K clears any leftover from a longer frame.
            self.stream.write("\r" + line + "\x1b[K")
        else:
            self.stream.write(tail + "\n")
        self.stream.flush()

    def close(self) -> None:
        # Final draw, then move off the bar line on a terminal.
        self._render(time.monotonic())
        if self.tty:
            self.stream.write("\n")
        self.stream.flush()
