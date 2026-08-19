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
| [0009](0009-product-name-and-package-name.md) | The product is called Trove; the package, CLI, and data directory are not | **Superseded by [0016](0016-rename-everything-to-trove.md).** Recorded the period when the rename was user-visible only and `organize_archive`/`oa` stayed as the identifiers. |
| [0010](0010-typing-two-lists.md) | Typing is two lists that move in opposite directions | The gradual-typing strategy that got the package fully checked: a "not reviewed yet" (`ignore_errors`) list that only shrank and a strict list that only grew, one package moving between them per commit. **Completed 2026-08-04** — both lists are retired and the strict settings are `[tool.mypy]` globals; see the amendment for what it cost and what it found. |
| [0011](0011-docstring-rules-scope.md) | Documenting a package's public surface is separate work from typing it | Ruff's `D101`/`D103` exemption list is not the same shape as mypy's strict list — `config/` and `pipeline/` are fully typed but still exempt, because a docstring written only to satisfy a linter teaches readers to skim past docstrings here. |
| [0012](0012-no-typeddict-for-mutation-results.md) | No TypedDict for the mutation results | The `{"ok": True, ...}` / `{"error": ...}` dicts write endpoints return stay `dict[str, Any]`, since their payload keys differ per call site; `services/types.py` only names shapes that more than one place actually builds. |
| [0013](0013-merge-tiebreak-is-the-lower-id.md) | A merge tie is broken by the lower id, for every entity | People, pets and places all fall to the lower id when their ranking ties, so a drag-merge no longer depends on which card was dragged onto which. The ranking itself stays per-module; only the last step is shared. |
| [0014](0014-node-22-for-the-desktop-toolchain.md) | The desktop toolchain is pinned to Node 22 | `.nvmrc`, `engines` and the docs all say 22, because Electron's `postinstall` unpacks nothing on Node 26 and still exits 0 — a broken checkout that `make check` reports as clean. |
| [0015](0015-per-archive-features.md) | An archive chooses its features, and the pipeline is the only gate | The feature set lives on the archive's registry entry; `stage_states` leaves a disabled stage out of the list, and everything else — not scheduled, no weights downloaded, no card, no nav section — follows from that one omission. Switching a feature off deletes nothing. |
| [0016](0016-rename-everything-to-trove.md) | The package, CLI, application id and data directory are all Trove too | Reverses [0009](0009-product-name-and-package-name.md): one name everywhere. `oa` and `OA_LOG_LEVEL` are hard breaks; the data directory is not — `app_data_migration` moves a pre-rename one on startup and repoints the absolute paths inside `config.json`. |
| [0017](0017-reading-text-inside-files.md) | Reading the text inside files, in one fused stage, with one dependency | The two text features share one open of a file, because whether a PDF needs reading as pictures cannot be known until its text layer has been tried; `doc_text.wanted` is what makes switching the second half on re-read the files the first could not. One dependency (`pypdfium2`) — the six office formats are zipped XML the standard library reads. A passage is sized for the person reading the result, and the record also says why the obvious "just embed it with SigLIP" is a plausible-looking wrong answer. |
| [0019](0019-reading-pixels-at-two-resolutions.md) | Reading picture text runs at two resolutions, and arbitrates per page | The planned "cheap detection gate" was premised on detection being far cheaper than recognition; measured, detection *is* the cost (1.47s vs a 1.38s full pass on a photograph). Detection now runs on a 736px copy and recognition on crops of the original — byte-identical text, and a text-free picture drops from 1.51s to 0.59s. A page is read as pixels only when it has almost no text AND a large image, because a title page has the first without the second. Models ship inside the wheel. |
| [0020](0020-three-rankings-not-four-routes.md) | Browse groups results by ranking, not by reader | Four readers fill indexes; three rankings answer a query. Search by document text and Search by picture text both feed one text ranking — they write into the same passages, so a hit's reader is a property of the file rather than of a separate search — and a reader is reported per *result* (`reader`) while a ranking gets the group. Each group takes the label and mark of the feature that produced it, composed by `features.search_ways` rather than worded again on a fourth surface. File names now run on every search rather than as a fallback, because being a fallback meant switching on Search by description silently removed them. |
| [0021](0021-a-feature-is-named-for-what-it-lets-you-do.md) | A feature is named for what it lets you do, never for what it reads | `card_label` kept one name per feature across four surfaces but said nothing about consistency between features, and eight labels were written to three rules — a process, a collection, a thing you can do, and two named after the *files they read*. "Documents" was the wrong name for a half that cannot read a scanned document, and needed a disclaimer in its own detail text to say so; the code had accumulated four wordings for the pair. A feature unlocking a section is named after the section; a feature that only widens the search box is named `Search by ‹what you type against›`. Mechanism words stay in `verb`/`noun`, where the running line already used them. Rejects naming every feature after its mechanism: four labels double as nav section names. |
| [0022](0022-cached-near-duplicate-pairs.md) | Duplicate groups are rebuilt wholesale; the search behind them is cached | A dedup rebuild spent 1,165 of its 1,168 seconds rediscovering which perceptual fingerprints are near which — repeated in full because five singleton files had been deleted — and the loop doing it had no cancellation checkpoint, so a cancelled job ran on with a frozen progress bar and recorded nothing. The BK-tree behind it visited ~10,000 nodes per lookup, not the "few dozen" claimed: unrelated 64-bit hashes sit ~32 bits apart, so a radius-6 window prunes almost nothing. Replaced by an exact banded index (pigeonhole over `threshold + 1` bands), with the pairs it finds cached in `dup_edges` against the content SHA they were found for. Groups are still rebuilt from scratch every run — 1,165s to 3.2s, same 18,916 groups, same canonicals. Rejects incremental *group* rebuilding: it would save seconds and cost the self-healing property 0006 rests on. |
| [0023](0023-an-action-is-asked-for-or-offered.md) | An action is either asked for or offered, and each has one control | [0002](0002-no-frontend-framework.md) leaves nowhere a control *lives*, so a contributor adding one opens the stylesheet named after their screen — and five "offered" buttons had drifted to three radii, three colours and three weights, with `.manage-features` at `.btn`'s weight reading as a main action beside the panel's real one. Four back controls were drawn three ways, one of them a bare text glyph with no accessible name. Three tiers now: `.btn`/`.btn.sec` for an action being asked for, `.quietbtn` for one being offered, `.linkbtn` inside a sentence — plus one `backControl()`, a quiet chevron whose destination lives in the tooltip. A new control is a modifier until it can say what makes it a different kind of thing. Rejects a component library ([0002](0002-no-frontend-framework.md)) and a stylelint rule, since "is this a new kind of control" is a judgement rather than a yes or a no. |
