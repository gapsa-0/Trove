"""The two structures near-duplicate grouping is built on: a fingerprint index
and a union-find.

Both are pure in-memory algorithms over integers -- no database, no Pillow, no
configuration. They are here rather than inline in ``exact.py`` because the
index in particular is the piece whose choice of data structure decides whether
a 90,000-photo archive is grouped in a minute or in twenty, and that is a
decision worth being able to read (and test) on its own.
"""

from __future__ import annotations

from collections.abc import Iterable

# Fingerprints are 64-bit (``phash64``); the band layout below splits exactly
# this many bits.
HASH_BITS = 64


class UnionFind:
    """Disjoint sets over file ids, with path halving."""

    def __init__(self, values: Iterable[int]) -> None:
        self.parent = {v: v for v in values}

    def find(self, value: int) -> int:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, a: int, b: int) -> None:
        a, b = self.find(a), self.find(b)
        if a != b:
            self.parent[b] = a


def band_layout(threshold: int) -> list[tuple[int, int]]:
    """``(offset, width)`` per band, splitting 64 bits into ``threshold + 1``.

    That count is the whole trick, and it is a pigeonhole argument: two
    fingerprints at most ``threshold`` bits apart differ in at most
    ``threshold`` bits, those bits can fall in at most ``threshold`` distinct
    bands, so with ``threshold + 1`` bands at least one band must be *bit for
    bit identical*. Indexing every band therefore finds every true match --
    there are no false negatives to trade away, which is what makes this a
    drop-in replacement for a structure that compares distances directly.

    Widths differ by at most one bit so no band is disproportionately wide (a
    wide band is a sparse one, and a sparse band contributes nothing but its
    own lookup). Clamped at 64 bands: past that the widths would round to zero,
    and a threshold that high matches most of an archive to most of it anyway.
    """
    count = max(1, min(threshold + 1, HASH_BITS))
    width, remainder = divmod(HASH_BITS, count)
    offset, layout = 0, []
    for i in range(count):
        this = width + (1 if i < remainder else 0)
        layout.append((offset, this))
        offset += this
    return layout


class BandIndex:
    """Every fingerprint within ``threshold`` bits of a query, without
    comparing against them all.

    The structure this replaced was a BK-tree, which narrows a search using the
    triangle inequality of Hamming distance. That is the textbook answer and it
    is a poor one here: unrelated 64-bit perceptual hashes sit around 32 bits
    apart, so a radius-6 window excludes almost no branch, and a lookup on this
    project's own 88,000-photo archive was measured visiting ~10,000 nodes --
    against the "few dozen" the approach promises. The whole pass cost 1,165
    seconds. Banding the same archive costs 63.

    Both structures return exactly the same matches; see ``band_layout`` for
    why banding cannot miss one.
    """

    def __init__(self, threshold: int) -> None:
        self.threshold = threshold
        self._bands = band_layout(threshold)
        self._buckets: list[dict[int, list[int]]] = [{} for _ in self._bands]
        self._values: dict[int, int] = {}

    def add(self, value: int, file_id: int) -> None:
        self._values[file_id] = value
        for (offset, width), buckets in zip(self._bands, self._buckets, strict=True):
            buckets.setdefault((value >> offset) & ((1 << width) - 1), []).append(file_id)

    def within(self, value: int) -> list[int]:
        """File ids whose fingerprint is at most ``threshold`` bits from ``value``.

        A file added to the index and then looked up finds *itself* at distance
        0, since nothing here knows which id the query came from. Callers
        exclude their own id.
        """
        candidates: set[int] = set()
        for (offset, width), buckets in zip(self._bands, self._buckets, strict=True):
            candidates.update(buckets.get((value >> offset) & ((1 << width) - 1), ()))
        return [
            fid for fid in candidates if (self._values[fid] ^ value).bit_count() <= self.threshold
        ]
