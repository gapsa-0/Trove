# 0010. Typing is two lists that move in opposite directions

- **Status:** Accepted, and completed 2026-08-04 — the two lists reached their
  end state and were retired. The strategy is the decision; the amendment at
  the bottom records how it ended and what replaced it. Read both.
- **Date:** 2026-08-01

## Context

`mypy trove` runs as a CI gate, and a gate that is sometimes red
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

Counting at the time of writing: 56 of the package's 108 modules are in the
strict list, and `grep -rn "type: ignore" trove/` returns nothing —
the strict packages are strict with no local escape hatch, not
strict-with-exceptions.

## Consequences

- `mypy trove` is green at every commit, which is what makes it
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

---

## Amendment, 2026-08-04: both lists are gone

The strategy above ran to its end. This section is the trail — what the lists
cost, what suppression was hiding, and why there is nothing to look at in
`pyproject.toml` now.

### What it cost

Measured with the pinned `mypy==2.3.0`, counting only errors located inside
each package rather than in whatever it imports:

| | Errors | Notes |
| --- | --- | --- |
| Emptying `ignore_errors` | 34 | across 7 packages; `dedup` was already at zero |
| Making the whole package strict | 305 | 92% of them `no-untyped-def` |

The second number is the interesting one. **Nearly all of the "strict" work was
writing a signature** — not restructuring code, not fighting the checker. The
original 102-error measurement made the job look like a rewrite; it was mostly
transcription, and that estimate is a large part of why it stayed un-started
for as long as it did. If a similar migration is proposed here again, measure
the error *kinds* before concluding it is expensive.

### What suppression was hiding

`ignore_errors` was the real debt, not the lenient default. A leniently checked
package is in a defensible state; a suppressed one is not, because nobody can
tell whether it holds one error or a hundred. Emptying the list turned up one
live bug: `faces/fiqa.py`'s composite scorer read its inputs with `getattr`,
but `retier_all` hands it `sqlite3.Row` objects, which have no attributes at
all. Every pre-AdaFace face therefore scored 0.0 and was tiered `LOW_QUALITY`
whatever it actually looked like, and `retier_all`'s `det_score AS score`
aliases were dead code. Fixed with a regression test ahead of the typing work,
per CONTRIBUTING. The existing test asserted `quality_tier in TIERS`, which a
score of 0.0 satisfies — an assertion loose enough that it could never fail is
not coverage.

### The resting state

Every module in `trove` is checked at what used to be the strict
block's settings, so they are declared as `[tool.mypy]` globals and there is no
override block at all. That is deliberately stronger than "the strict list
names every package": a list has to be *extended* for a new package, and one
added tomorrow without that line would be silently unchecked with a green
build. Now there is nothing to extend and nothing to forget.

`tests/unit/test_typing_is_global.py` keeps it that way. It asserts the strict
settings are still globals, that no override relaxes a package back to
unchecked, and that every `# type: ignore` names an error code. It mirrors
`test_layering.py`'s third property, which exists against the same failure
shape: a rule a new file can escape by simply not being mentioned.

### The escape hatches, and why there are exactly six

`grep -rn "type: ignore" trove/` used to return nothing, and that
was worth saying while only half the package was checked. It returns six now,
five of which are one pattern: an optional native dependency rebound to `None`
when absent (`cv2 = None`, `np = None` in the three model backends). The
alternative — typing `np` as `Module | None` — would make every real use of
numpy in those files unchecked, which is backwards. The sentinel is a runtime
concern; the module type is the true one everywhere past `available()`. The
sixth is `detect/video.py` coercing a nullable `media_meta` column inside the
`try` that exists to catch exactly that.

`warn_unused_ignores` is on, so an ignore that stops being needed fails the
build rather than accumulating.

### Two things settled along the way

- **numpy is typed, and that changed the answer.** The pre-work assumption was
  that arrays cross an `ignore_missing_imports` boundary and are therefore
  `Any`, which would have argued for an `Array = Any` alias to keep signatures
  readable. Measured instead: numpy, cv2, PIL, tokenizers and faiss all ship
  `py.typed` and are checked for real; only onnxruntime, insightface and
  scikit-learn are opaque. So array-shaped values say `np.ndarray` and mean it,
  and the alias idea moved to the boundary that actually is `Any` — `Session =
  Any`, defined in each backend that holds an ONNX session, as documentation
  rather than enforcement.
- **`trove/progress.py` exists because typing needed it.** Five
  packages report progress to one of two concrete trackers, both of which live
  above them in the layering. The `Protocol` that pins their shape had already
  been written once, privately, inside `metadata/enrich.py`; four more copies
  was the alternative to promoting it to L0. See `tests/unit/test_layering.py`.
