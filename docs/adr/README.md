# Architecture decision records

An ADR here records a decision that is **already implemented in the shipping
code** — not a proposal, not a plan. Each one exists to answer "why does the
code do it this way" for a decision non-obvious enough that a future change
could plausibly get it wrong (or undo it) without knowing the reasoning that
led here. Read the cited files before changing the area an ADR covers; if the
code has since moved on from what an ADR says, the ADR is stale and should be
corrected or superseded, not treated as authoritative over the code.

## Numbering

ADRs are numbered sequentially (`0001`, `0002`, ...) in the order they were
written, not in the order the decisions were made or shipped. A number is
never reused or renumbered, even if the decision it records is later
reversed — a reversal gets its own new-numbered ADR that supersedes the old
one, so the history of "we tried X, then switched to Y" stays legible.

## Format

```
# NNNN. <Title>

- **Status:** Accepted
- **Date:** YYYY-MM-DD

## Context
## Decision
## Consequences
```

## Records

| # | Title | Summary |
| --- | --- | --- |
| [0001](0001-sqlite-per-archive.md) | One SQLite database and cache per archive | Each GUI archive is fully isolated under `archives/<id>/`; only model weights and the app icon are shared. The CLI stays on one shared catalogue. |
| [0002](0002-no-frontend-framework.md) | No frontend framework, no bundler | The web UI is vanilla ES modules with no build step; `tools/dev/check_handlers.py` covers the one gap eslint can't. |
| [0003](0003-local-only-ml.md) | All machine learning runs locally, on onnxruntime | SigLIP 2, SCRFD/AdaFace, and YOLOX/DINOv2 all run through onnxruntime; torch and transformers are excluded from the packaged build; the retired cloud (Voyage) path is actively cleaned up, not just unused. |
| [0004](0004-fused-detection.md) | People and pets are detected in one decode pass, and arbitrate each other | One shared image decode feeds both detectors; the YOLOX `person` box arbitrates People-vs-Pets in both directions and resolves true orientation. One stage, one UI card — do not re-split. |
| [0005](0005-pipeline-status-in-memory.md) | Pipeline status is in-memory, not persisted | One in-memory job registry is the source of truth for `/api/pipeline`; only the pause flags (whole-pipeline and per-stage) are persisted, in `config.json`, because they are user intent rather than status. |
| [0006](0006-flag-and-group-dedup.md) | Deduplication flags and groups; it never deletes | Exact (SHA-256) and perceptual duplicate groups are flagged, with one canonical copy chosen by a deterministic rule; non-canonical copies are hidden, never deleted. Analytics counts every present file; only Browse hides duplicates. |
| [0007](0007-python-3-13-floor.md) | The Python floor is 3.13 | `pyproject.toml`, mypy, ruff and CI all pin 3.13, with no compatibility matrix — the packaged app bundles its own interpreter, so the floor only affects building from source. |
| [0008](0008-manual-tags-anchored-by-name.md) | Manual person/pet tags are anchored by name, not by id | Clustering rebuilds `persons`/pet identities wholesale, so manual tags reference a name and a repair step re-points them after every re-cluster; only named people/pets can carry a manual tag. |
| [0009](0009-product-name-and-package-name.md) | The product is called Trove; the package, CLI, and data directory are not | The rename is user-visible only — `organize_archive`, `oa`, the app id, and the data directory are unchanged so upgraded installs keep working. |
| [0010](0010-typing-two-lists.md) | Typing is two lists that move in opposite directions | mypy's "not reviewed yet" (`ignore_errors`) list only shrinks and its strict list only grows; a package moves between them in one commit, which is what keeps `mypy organize_archive` green at every commit instead of a report nobody reads. |
| [0011](0011-docstring-rules-scope.md) | Documenting a package's public surface is separate work from typing it | Ruff's `D101`/`D103` exemption list is not the same shape as mypy's strict list — `config/` and `pipeline/` are fully typed but still exempt, because a docstring written only to satisfy a linter teaches readers to skim past docstrings here. |
| [0012](0012-no-typeddict-for-mutation-results.md) | No TypedDict for the mutation results | The `{"ok": True, ...}` / `{"error": ...}` dicts write endpoints return stay `dict[str, Any]`, since their payload keys differ per call site; `services/types.py` only names shapes that more than one place actually builds. |
| [0013](0013-merge-tiebreak-is-the-lower-id.md) | A merge tie is broken by the lower id, for every entity | People, pets and places all fall to the lower id when their ranking ties, so a drag-merge no longer depends on which card was dragged onto which. The ranking itself stays per-module; only the last step is shared. |
