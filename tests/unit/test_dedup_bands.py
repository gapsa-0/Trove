"""The near-duplicate index must agree with brute force, exactly.

`BandIndex` replaced a BK-tree for speed, and the only thing that makes that
swap safe is that it returns the *same* matches rather than a fast
approximation of them. Every test here is that one property asked a different
way, because a band layout that drops a true match would silently stop grouping
re-saved copies -- with nothing failing, and no way to notice short of
comparing a run against an older one.
"""

from __future__ import annotations

import random

from trove.dedup.bands import HASH_BITS, BandIndex, UnionFind, band_layout


def _brute(values: dict[int, int], query: int, threshold: int) -> set[int]:
    return {fid for fid, v in values.items() if (v ^ query).bit_count() <= threshold}


def _flip(value: int, bits: int, rng: random.Random) -> int:
    for position in rng.sample(range(HASH_BITS), bits):
        value ^= 1 << position
    return value


def test_band_layout_covers_all_64_bits_without_overlap():
    for threshold in range(0, 20):
        layout = band_layout(threshold)
        assert sum(width for _, width in layout) == HASH_BITS
        assert [offset for offset, _ in layout] == [
            sum(w for _, w in layout[:i]) for i in range(len(layout))
        ]
        # One band more than the threshold is what makes a missed match
        # impossible; see band_layout's docstring for the pigeonhole argument.
        assert len(layout) == min(threshold + 1, HASH_BITS)


def test_every_match_brute_force_finds_is_found():
    rng = random.Random(20260809)
    threshold = 6
    values = {}
    for file_id in range(1, 1201):
        # A mix of unrelated pictures and near-copies at every distance in and
        # around the threshold, so the assertion covers both sides of the line.
        if values and file_id % 3 == 0:
            base = values[rng.choice(list(values))]
            values[file_id] = _flip(base, rng.randrange(0, threshold + 3), rng)
        else:
            values[file_id] = rng.getrandbits(HASH_BITS)

    index = BandIndex(threshold)
    for file_id, value in values.items():
        index.add(value, file_id)

    for file_id, value in values.items():
        assert set(index.within(value)) == _brute(values, value, threshold), file_id


def test_a_threshold_of_zero_matches_only_identical_fingerprints():
    index = BandIndex(0)
    index.add(0xDEADBEEFCAFEF00D, 1)
    index.add(0xDEADBEEFCAFEF00C, 2)  # one bit away
    index.add(0xDEADBEEFCAFEF00D, 3)  # the same picture again

    assert sorted(index.within(0xDEADBEEFCAFEF00D)) == [1, 3]


def test_an_empty_index_matches_nothing():
    assert BandIndex(6).within(0) == []


def test_a_fingerprint_finds_itself_so_callers_can_exclude_it():
    """Documented in `within`, and relied on by edges.py, which drops its own id
    rather than keeping a second structure to ask what it already knows."""
    index = BandIndex(6)
    index.add(42, 7)

    assert index.within(42) == [7]


def test_union_find_merges_through_a_chain():
    uf = UnionFind([1, 2, 3, 4])
    uf.union(1, 2)
    uf.union(3, 4)
    assert uf.find(1) != uf.find(3)

    uf.union(2, 3)
    assert len({uf.find(i) for i in (1, 2, 3, 4)}) == 1
