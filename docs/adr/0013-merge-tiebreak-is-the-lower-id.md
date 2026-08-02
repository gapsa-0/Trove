# 0013. A merge tie is broken by the lower id, for every entity

- **Status:** Accepted
- **Date:** 2026-08-01

## Context

Three entities can be merged by hand from the GUI: people, pets, and
places. Each ranks the two sides on what it has, and the higher-ranked
side survives — a named side beats an unnamed one everywhere, then
`face_count` for people, `detection_count` for pets, and pinned-then-
`member_count` for places.

What happens when those are equal was never decided; it was whatever each
module's comparison chain happened to do. Pets and places both ended on
`pa["id"] < pb["id"]`, so the lower id survived. People ended on
`(pa["face_count"] or 0) >= (pb["face_count"] or 0)` — a `>=` with nothing
after it — so the *first argument* survived. Nothing tested any of this
until `4d22f60`, which pinned all three as characterisation tests during
the `services/` refactor without changing them.

The people case is not just an inconsistency. `merge_persons` is reached
by dragging one person card onto another, and which id the GUI sends first
is which card the user picked up. So merging two equal-sized unnamed
clusters kept a different person depending on the direction of the drag,
and the two outcomes differ in what the undo record points at.

## Decision

All three break a tie on the lower id. `merge_persons` gains the same
`elif pa["id"] < pb["id"]` step pets and places already had, and its `>=`
becomes a `!=` comparison so the count test and the tiebreak are separate
steps rather than one conflated one.

The survivor rule itself stays per-module. `services/merging.py` shares the
mechanics of a linked merge — move the children, delete the loser, write
the undo record — and takes an already-chosen survivor; it does not choose.
Hoisting the choice would mean one generic function carrying three
entities' notions of "bigger", which is what that module's docstring
declines to do. What is now shared is only the last step, and it is stated
in each module next to the ranking it completes.

## Consequences

- A drag-merge's outcome no longer depends on the direction of the drag.
- `test_merge_persons_equal_count_tie_keeps_first_argument` asserted the
  old behaviour and is now
  `test_merge_persons_equal_count_tie_keeps_the_lower_id`, checking both
  argument orders like its pets and places counterparts. It was updated,
  not deleted: the behaviour it described was real.
- This is a behaviour change, not a refactor. Existing merges are
  unaffected — the rule only runs when a merge is performed, and past
  merges keep whatever survivor they were given.
- A fourth entity type gets the same shape: rank on what you have, then
  fall to the lower id. A tiebreak that leaves argument order deciding is
  the bug this closes, not a style choice.
