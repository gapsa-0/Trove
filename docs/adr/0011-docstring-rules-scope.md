# 0011. Documenting a package's public surface is separate work from typing it

- **Status:** Accepted
- **Date:** 2026-08-01

## Context

Ruff's `D100`/`D101`/`D103`/`D104` docstring rules (module, public class,
public function, package) are selected for the whole library in
`[tool.ruff.lint]`. `D100` and `D104` — "does this module/package say what it
is" — are satisfied everywhere already and carry no per-file exemption. The
two expensive rules, `D101` (public class) and `D103` (public function), are
different: they are turned off for a specific, named list of packages in
`[tool.ruff.lint.per-file-ignores]`, and that list is not the same shape as
mypy's strict-typing list from 0010. `organize_archive/config/*.py` and
`organize_archive/pipeline/*.py` are both in mypy's strict block — fully
typed, `disallow_untyped_defs` and all — and both still appear in the
`D101`/`D103` exemption list, alongside `paths.py` and packages like `dedup/`,
`faces/`, and `web/*.py` that are not mypy-strict at all.

Lifting the exemption for just `config/`, `pipeline/`, and `paths.py` — the
part of the exemption list that overlaps mypy's strict block — and running
`ruff check --select D101,D103` against them today reports 18 missing
docstrings, mostly on stage `run()` functions and small dataclasses like
`pipeline/stages.py`'s `StageDef` whose fields are already documented inline
with comments.

## Decision

The two lists stay separate on purpose. A package being mypy-strict says
nothing about whether `D101`/`D103` are enforced there, and vice versa:
`services/`, `db/`, `metadata/`, and `web/routes/` have both a typed and a
documented public surface, but `config/` and `pipeline/` are typed without
being required to carry a docstring on every public function and class.

Coupling the two lists — requiring every mypy-strict package to also clear
`D101`/`D103` — would force writing on the order of twenty docstrings whose
only audience is the linter. A docstring written to satisfy a rule instead
of a reader restates the signature (`"""Run the stage."""` above
`def run(ctx: JobContext) -> None`) and teaches the next person that
docstrings in this codebase are noise to skim past, which is a worse outcome
than the function having no docstring at all.

## Consequences

- Both lists only ever shrink (a line is removed once that package's public
  surface is actually documented, in the same commit), the same rule as
  0010's strict-typing list, but membership is independent: removing a
  package from the mypy "not reviewed yet" list does not remove it from the
  `D101`/`D103` exemption, and typing a package first (as `config/` and
  `pipeline/` already are) does not create pressure to document it next.
- A reader who sees `config/` or `pipeline/` fully typed but exempt from
  `D101`/`D103` should not read that as an oversight to "fix" by deleting
  the per-file-ignore line — the two are deliberately decoupled, and adding
  the docstrings only earns removing the line once they say something a
  reader couldn't get from the signature.
- `D102` (public method) is not in the selected rule set at all, for the
  same reason in miniature: per its own comment in `pyproject.toml`, the
  strict-documented packages already have no undocumented public methods,
  so turning it on today would add nothing — it is a candidate for later,
  once that stops being true.
