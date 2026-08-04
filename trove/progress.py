"""The shape of a progress tracker, as a structural type.

Long-running passes -- scan, enrich, dedup, detect, faces, pets -- all report
the same way, and all of them are handed one of two concrete trackers:
``cli.progress.ScanProgress`` when a person is watching a terminal, or
``pipeline.job.JobProgress`` when the GUI's scheduler is driving. Both live in
L3/L2, above every package that reports to them, so neither can be imported
for its type without inverting the layering the package is built on.

A Protocol is the answer: it pins the shape at the point of use without naming
either implementation. It lives here, at L0, because five packages need the
same one and the alternative is five copies of the paragraph below.
"""

from __future__ import annotations

from typing import Protocol


class Progress(Protocol):
    """What a pass may assume about the tracker it was handed."""

    total: int

    # done/bytes_hashed are positional-only on purpose: every call site passes
    # them positionally, and the two implementations disagree on the second
    # name (`bytes_hashed` vs `_bytes`), which a by-name protocol would reject.
    # `current` stays named -- scan/walker.py passes it by keyword.
    def update(self, done: int, bytes_hashed: int, /, current: str = "") -> None: ...
