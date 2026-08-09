# 0022. Duplicate groups are rebuilt wholesale; the search behind them is cached

- **Status:** Accepted
- **Date:** 2026-08-09

## Context

`dedup/exact.py` has always rebuilt every duplicate group from scratch on
every run, and [0006](0006-flag-and-group-dedup.md) treats that as a feature:
the pass is idempotent, so a grouping damaged by a crash or left behind by an
older version is repaired by the next run rather than persisting. Nothing here
argues with that.

What made it untenable was the cost. Measured on the maintainer's own archive
(97,083 files, 88,234 of them fingerprinted images) by the app's own log:

```
07:10:27  job start kind=dedup root=1 job=3
07:29:53  job done  kind=dedup root=1 job=3 ... elapsed=1165.4s
```

Nineteen minutes, of which the catalogue work — loading candidates, clearing
the old groups, writing the new ones, committing — was about twenty seconds.
The other 1,145 were spent deciding which perceptual fingerprints are within
`phash_hamming_threshold` bits of which, using a `_BKTree`.

Three separate problems fell out of that, all visible in one incident. A scan
found five deleted files — every one of them a singleton, belonging to no
duplicate group at all — and the rebuild that followed searched all 88,234
fingerprints again. Five minutes in, the app was closed; `shutdown` logged
`cancelling 1 running job(s): dedup` and then waited, because the loop doing
the searching contained no cancellation checkpoint and could not answer. The
job was killed without writing its `dedup_runs` marker, so the next launch was
owed the same nineteen minutes from zero. From the outside this looked like a
stage that ran forever and achieved nothing — the progress bar sat at the end
of the fingerprinting phase for the entire pass, because the search reported no
progress either.

The BK-tree itself was also simply the wrong structure, which is worth being
explicit about because it is the textbook answer and looks right. It narrows a
search using the triangle inequality of Hamming distance, which works when the
radius is small relative to the spread of the data. Unrelated 64-bit perceptual
hashes sit around 32 bits apart, so a radius-6 window excludes almost no
branch: instrumented on this archive, a single lookup visited **~10,000 nodes**,
against the "few dozen" `docs/duplicates.md` claimed. The whole pass measured
super-linear, roughly O(n^1.8) — 5.7s at 5,000 fingerprints, 236s at 40,000.

## Decision

**Groups stay wholesale. The search does not.**

Every run still clears and rewrites every duplicate group for the archive, in
one transaction, exactly as 0006 describes. What a run no longer redoes is
discovering which fingerprints are near which.

**The index is banded, not a tree** (`dedup/bands.py`). A 64-bit hash is split
into `threshold + 1` bands and every band is indexed. Two hashes at most
`threshold` bits apart differ in at most `threshold` bands, so by pigeonhole at
least one band matches *bit for bit* — indexing all of them finds every true
match with no recall traded away. This is exact, not approximate, which is what
makes it a drop-in replacement; `tests/unit/test_dedup_bands.py` asserts
agreement with brute force over a generated archive spanning both sides of the
threshold. On the real archive it searches all 88,234 fingerprints in 63
seconds against the BK-tree's 1,165.

**The pairs are cached** (`dedup/edges.py`, `dup_edges`). That two files are
within the threshold is a fact about their *content*, and content is what
`files.sha256` identifies, so the fact holds until one of the two files
changes. `dup_edge_scan` records, per file, which SHA it was searched for and
under which threshold — the same shape as the existing `face_scan`/`pet_scan`
tables. Any of the three ways that record can go stale (never searched, content
changed, threshold changed) makes that one file's search owed again and nothing
else's.

Deriving staleness from content rather than from a "what changed since?" diff
is the load-bearing part. A diff has to be right about deletions, re-appearances
and in-place edits all at once, and `dedup_needed`'s existing count/max-id
comparison already cannot tell "one file vanished and one arrived" from "nothing
happened". Per-file content marks cannot have that bug: there is no global
state to get out of step, and a file whose row is missing is simply owed a
search.

**Cancellation lives in the search loop.** It is now the only loop in the pass
whose length grows with the archive, and it reports progress every 100 files —
which is also the cancellation checkpoint, since `JobProgress.update` raises on
the cancel event. Everything else in a run is sub-second.

### What was rejected

- **Rebuilding groups incrementally too** — computing the working set of groups
  a change can affect, merging into them, and leaving the rest untouched. This
  is exact (additions only merge components; removals only split them, and a
  group is a maximal component so a split is local), and it would have saved
  the remaining few seconds. It was rejected because it trades away the
  self-healing property 0006 relies on: a group written wrong would stay wrong
  until something happened to touch it. Caching the *search* gets essentially
  the whole win — 1,165s to 3.2s on an unchanged archive — while every group in
  the catalogue is still re-derived from scratch on every run. Cheapness was
  not worth the invariant.
- **Storing only a spanning forest of the pairs** rather than all of them.
  Smaller, but deleting a file could then disconnect a group that is in fact
  still connected through a pair nobody wrote down. The full set is 50,178 rows
  on this archive; the saving was not real.
- **Deleting pairs for absent files.** `load` filters on `present=1` instead.
  An unplugged drive comes back, and its groups should come back with it rather
  than costing a re-search.

## Consequences

- A run over an unchanged archive does no searching at all. Measured end to
  end on a copy of the real archive: **1,165s → 3.2s**; the reported
  five-deletions case → **5.4s**; twenty new files → **4.6s**. The first run
  after upgrading pays 56s once to fill `dup_edges`, then never again.
- The grouping is unchanged, and this is checked rather than assumed: the new
  pass reproduces the old one's 18,916 groups, 27,317 hidden files, member
  partition and canonical picks exactly.
- Changing `phash_hamming_threshold` invalidates every stored pair, so the run
  after a threshold change costs a full search. This is correct — the old pairs
  answer a different question — but it is a config change with a visible price,
  which it did not have before.
- Losing the media extra no longer collapses existing near-duplicate groups.
  Fingerprinting stops, so no *new* near-duplicates are found, but the pairs an
  earlier run paid for are still applied. That is a behaviour change, and the
  better reading of "the extra buys near-duplicate detection".
- `dedup/` is now five modules rather than one, because the file grew past the
  600-line limit and, more usefully, because the index is worth reading and
  testing on its own. `exact.py` keeps the name and the public surface
  (`run`, `DedupStats`, `perceptual_available`) so no caller moved; the name is
  now a poor description of a module that orchestrates exact *and* perceptual
  grouping, and renaming it is owed.
- One latent bug was fixed on the way past: `groups.clear` relied on
  `ON DELETE CASCADE` to clear `dup_members`, which only fires where
  `PRAGMA foreign_keys` is on. Correctness that turns on a connection-level
  setting is one stray connection away from silently accumulating members of
  groups that no longer exist, so the sweep is explicit now.
