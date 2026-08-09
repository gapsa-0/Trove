# 0006. Deduplication flags and groups; it never deletes

- **Status:** Accepted
- **Date:** 2026-08-01

## Context

The archive this tool catalogues is largely Google Takeout exports from
several family members, plus phone dumps and event folders, so the same
photo commonly appears many times over — once per person's export, often
re-compressed or renamed by Google along the way. Cross-copy duplication is
the core problem this tool exists to surface, and the archive is read-only:
whatever the answer is, it cannot involve deleting or rewriting a source
file.

## Decision

`trove/dedup/exact.py` groups duplicates in two layers and never
deletes anything. Its module docstring states the invariant directly: "Each
group keeps one **canonical** copy visible; the rest are marked hidden
(never deleted, always reviewable)."

**The two layers.**

- **Exact**: SHA-256 of file content, byte-identical copies. A cheap
  partial-hash prefilter (`fast_hash_sample_bytes`, a head+tail sample) is
  applied before the full hash to avoid reading every byte of every
  candidate.
- **Perceptual / near-duplicate**: image perceptual hashing
  (`perceptual_available()` gates this on `imagehash` + Pillow being
  installed), grouped by Hamming distance, which would otherwise turn a
  150k-photo archive into billions of checks.

  > **Note, 2026-08-09.** The structure that narrows those checks was a
  > `_BKTree` when this was written; it is now `dedup/bands.py`'s `BandIndex`,
  > and the pairs it finds are cached in `dup_edges` rather than rediscovered
  > every run. Both changes are covered by [0022](0022-cached-near-duplicate-pairs.md)
  > and neither changes the grouping — the replacement was verified to
  > reproduce this archive's 18,916 groups, hidden set and canonical picks
  > exactly. The decision below is untouched.

Groups are built with a `UnionFind` over both signals together, so a chain
of exact and perceptual matches collapses into one group regardless of which
relation connects any two members.

**Canonical selection**, read in the exact order `_pick_canonical` applies
it (a `min()` over a tuple key, so each criterion only breaks ties left by
the one before it):

1. **Highest pixel count** (`width * height`) — highest resolution wins.
2. **Largest file size** — a tie-break for least-compressed / largest byte
   size among same-resolution copies.
3. **Presence of a Takeout sidecar** (`has_side`, whether the file has a
   matched `takeout_sidecar` row) — richer provenance wins.
4. **Presence of a resolved date** (`has_date`, whether the file has a
   `dates` row).
5. **Earliest resolved `best_datetime`** — among otherwise-tied copies, the
   one that resolves to the earliest capture date.
6. **Stable path order**, then **file id** — a deterministic final
   tiebreaker so re-runs never pick a different canonical file among truly
   identical candidates.

This differs from the naive "highest resolution, then largest size, then
richer metadata, then earliest date, then stable path" description one might
expect from memory only in being explicit about what "richer metadata" is
(a sidecar row, then a resolved date row, as two separate criteria) and in
adding the file id as a final tiebreaker after path — read the actual `key=`
tuple in `_pick_canonical` for the authoritative order if this ever needs
retuning.

**Analytics vs. Browse.** `trove/services/_common.py` defines the
distinction the rest of the services layer builds on: `_VISIBLE = "f.present
= 1"` counts every present file, while `_NOT_HIDDEN = "f.present = 1 AND
f.hidden = 0"` additionally excludes non-canonical duplicates. The comment
above both constants states the rule plainly: "Analytics (summary, timeline,
map, counts) describe the whole archive, so they count every present file.
Only Browse hides non-canonical duplicates." Every other duplicate copy in a
group is marked `hidden` on its `files` row and carries `dup_group_id`, but
is never deleted and always reviewable — the Duplicates page exists
specifically to browse and, if wanted, un-hide them.

## Consequences

- Running dedup twice on an unchanged archive produces the same groups and
  the same canonical picks — the whole pass is idempotent and safe to
  re-run after any change to the file set (`_pick_canonical` is
  deterministic by construction).
- The tool never destroys data. A future optional command may emit a
  user-run cleanup or hardlink plan for someone who wants to reclaim disk
  space, but the tool itself still would not perform the deletion — that
  stays outside this decision's scope.
- Any query counting "how many photos/videos/duplicates" needs to know
  which of the two predicates it wants; conflating them would either
  under-count the archive (using `_NOT_HIDDEN` for a total) or make
  duplicate copies appear in Browse (using `_VISIBLE` there).
