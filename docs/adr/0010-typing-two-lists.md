# 0010. Typing is two lists that move in opposite directions

- **Status:** Accepted
- **Date:** 2026-08-01

## Context

`mypy organize_archive` runs as a CI gate, and a gate that is sometimes red
is worse than no gate: it trains everyone to ignore its output. Turning
strict checking (`disallow_untyped_defs` and friends) on for the whole
package at once was tried as a measurement, not a plan — at commit
`48f949a`, the lenient default alone (annotated code checked, unannotated
functions left alone) already produced 102 errors across the codebase. A
whole-package strict switch would have added a much longer report that
nobody reads, in place of a gate that is green and stays green.

## Decision

`[tool.mypy]` in `pyproject.toml` keeps two `[[tool.mypy.overrides]]` blocks
that move in opposite directions:

- A **"not reviewed yet"** block (`ignore_errors = true`) listing the
  packages known to have pre-existing errors under the lenient default —
  `dedup`, `detect`, `embeddings`, `faces`, `geo`, `pets`, `thumbnails`, and
  `web` (all as `.*` globs). This list only ever **shrinks**.
- A **strict** block (`ignore_errors = false`, `disallow_untyped_defs = true`,
  `disallow_incomplete_defs = true`, `strict_optional = true`,
  `warn_return_any = true`) listing the packages that have actually been
  brought clean — `paths`, `errors`, `metadata.*`, `config.*`, `db.*`,
  `services.*`, `web.routes.*`, and `pipeline.*`. This list only ever
  **grows**.

A package moves from the first list to the second in the single commit that
makes its errors actually go away — never partially, never in advance of the
work. Section order in the file is load-bearing: mypy applies every matching
override in file order, so the strict block has to come after the ignored
one and say `ignore_errors = false` explicitly, which is what lets
`web.routes.*` be strict while its parent `web.*` is still on the lenient
list.

Counting today: 56 of the package's 108 modules are in the strict list, and
`grep -rn "type: ignore" organize_archive/` returns nothing — the strict
packages are strict with no local escape hatch, not strict-with-exceptions.

## Consequences

- `mypy organize_archive` is green at every commit, which is what makes it
  usable as a CI gate instead of a report someone has to remember to read.
- Adding a package to the strict list is a one-way door in practice: nothing
  stops errors from creeping back in later, but nothing routine drags the
  list backwards either, since packages only move forward once clean.
- A package can only leave the "not reviewed yet" list once it is fully
  clean under the strict settings — there is no partial-credit state, so
  "clean up half of `web/`" is not representable; the unit of progress is
  one whole package.
- The two lists are a map of remaining work, not a target: shrinking the
  first list is real progress, but there is no gate demanding it shrink on
  any schedule.
