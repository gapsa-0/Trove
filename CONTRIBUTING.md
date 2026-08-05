# Contributing

Operational notes for working in this repository. For what the pieces are and how
they fit together, see [ARCHITECTURE.md](ARCHITECTURE.md).

## Setup

```
make setup
make check
```

is the whole onboarding sequence.

`make setup` creates a venv with `python3.13 -m venv .venv`, upgrades pip inside it,
installs the package editable with every extra (`pip install -e '.[dev,cli,media,faces,pets,semantic,documents,ocr]'
-c constraints.txt`), installs the pre-commit hook, and runs `npm ci` in `desktop/`.
If your machine has no `python3.13` on PATH, point the venv step at whichever
interpreter you do have:

```
make setup PYTHON=/path/to/python3.13
```

`make check` is "everything CI runs": `lint` (Python static checks, then the
desktop JS lint), `handlers` (checks every inline `on*` handler — in the markup and in the
template literals the screen modules generate markup with — resolves to
something `main.js` actually exports; nothing else catches a handler that
silently does nothing when clicked), `sizes` (the file and function size
ratchet — see "Definition of done"), `test` (the suite, `.venv/bin/python -m
pytest -q -m "not browser"`), and `test-browser` (the frontend, in a real
headless Chrome — skipped automatically if none is installed).

For the day-to-day save loop, `make test-fast` runs the unit tier only, skipping
tests marked `slow`: `.venv/bin/python -m pytest -q -m "not slow" tests/unit`.
That drops both the sleeping tests and the ~13s SigLIP module (which lives in
`tests/unit` but is marked `slow`), landing around 2s. It does not run
`tests/integration` or `tests/gui`, so it is not a substitute for `make check`
before a commit.

**Trap:** `ruff` and `pre-commit` are installed only inside `.venv`, and
`pre-commit`'s importable module name has an underscore where the PyPI name
has a hyphen. Running `pre-commit` or `ruff` bare, or `python -m pre-commit`,
fails outside an activated venv or with the wrong module name. The commands
that actually work are:

```
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m pre_commit install
```

The Makefile always spells them this way (via `PY ?= .venv/bin/python`), which is
the reason to prefer `make lint` / `make fmt` over remembering the invocation.

## Commit rules

- Maintainer work lands directly on `main`; there are no long-lived feature
  branches. If you are contributing from a fork, open a pull request against
  `main` — everything below applies to its commits just the same.
- Imperative subject line, 72 characters or fewer.
- The body explains *why*, not what — the diff already says what changed.
- No mixed commits: one concern per commit. If you catch yourself writing "and"
  in the subject, it's probably two commits.
- A pure-move or pure-rename commit only moves or renames; say so in the subject
  or body (e.g. "no logic change"). Any accompanying fix is a separate, later
  commit.
- Every commit is green before it is made: run `make check` first.

Real examples from this repository's history:

```
Refuse a negative retry count instead of skipping the write
Type web/routes/ and put it in the strict mypy list
Give the shared People/Pets card pieces their own module
```

## Definition of done

A change is finished when all of these are true. The pull request template
repeats the list so it gets checked rather than remembered.

- **`make check` passes** — lint, format, types, the handler and size checks,
  and the full suite. Not "tests pass on my branch": the whole gate.
- **A bug fix carries a regression test that fails without the fix.** Write the
  test first and watch it fail; a test written after the fix pins the fix, not
  the bug.
- **A new feature has tests for its normal path and at least one edge case** —
  empty input, a missing file, a duplicate, whichever failure the feature is
  actually likely to meet.
- **No file over 600 lines and no function over 80.** `make sizes`
  (`tools/dev/check_sizes.py`) enforces it. There are currently no exceptions —
  its allowlist is empty, and keeping it that way is the goal. If a change
  genuinely needs an entry, say why in the commit body; that visible
  justification is the entire mechanism, and an entry that stops being needed
  has to be deleted.
- **A user-visible change updates `README.md` and/or the relevant page under
  `docs/`**, in the same commit or a following one in the same batch of work —
  not "later" — **and adds a `CHANGELOG.md` entry under `[Unreleased]`**.
  Releases are cut by moving that section, so an entry written later is an
  entry that misses its release.
- **New derived data carries its provenance**: which source produced it, and a
  confidence, stored alongside the value rather than implied by ordering.
- **A long operation is resumable and idempotent.** Verify it the only way that
  counts: kill it mid-run, run it again, and check it picks up rather than
  redoing or double-counting the work.
- **Nothing writes to a source archive root.** Ever — see the hard rules below.
- **A decision that closes off an alternative gets an ADR** under `docs/adr/`,
  so it is not re-argued in six months by someone who cannot see why the
  obvious-looking alternative was rejected.

## The routine

- **While working:** `make test-fast` (the unit tier, ~2s).
- **Before committing:** `make lint` — the pre-commit hook runs it anyway, but
  finding out before you write the message is cheaper.
- **Before pushing:** `make check`.
- **Per release:** the checklist in [`docs/release.md`](docs/release.md).
- **Quarterly:** an hour on [`docs/dev/repo-review.md`](docs/dev/repo-review.md)
  — what grew, what got slow, what died, what drifted from its ADR. A scheduled
  workflow opens the issue so it does not depend on anyone remembering.

## The hard project rules

- **Never write to, move, rename, or delete anything under a source archive
  root.** All output goes to the catalogue database and a separate cache
  directory. If a feature seems to need to touch a source root, that's a
  design smell — raise it before building it.
- **No network calls for media processing.** The one bounded exception is the
  map's street-map tile layer: it is a user-facing toggle, defaults on, and
  sends photo *coordinates* to a public tile server, never the photos
  themselves — turning it off leaves a fully offline plot (see `README.md`'s
  Privacy section). Local models' weights download once, on first use, and
  nothing after that — including the two self-exported ONNX files (AdaFace, the
  DINOv2 pet model), which resolve through `packaging/models/manifest.json` via
  `trove/model_manifest.py` and are SHA-256 verified before they are
  used. A fresh clone needs no model setup step: run the app and they arrive.
- **Long operations must be resumable and idempotent.** Scanning, hashing,
  detection, and embedding all need to be safe to interrupt and re-run,
  picking up where they left off rather than redoing finished work.
- **Every derived fact records which source produced it and a confidence** —
  see "Definition of done" above; this is the same rule stated as a design
  constraint rather than a checklist item.
- **Package layering is enforced by `tests/unit/test_layering.py`**, not just
  convention. A module may import from its own layer or any layer below it,
  never above — and a deferred import inside a function body counts just as
  much as a top-level one. Four layers, lowest first:

  | Layer | Name | Contains |
  | --- | --- | --- |
  | L0 | foundation | `config`, `paths`, `runtime`, `logging_setup`, `errors`, `db` |
  | L1 | domain | `scan`, `hashing`, `metadata`, `media`, `dedup`, `geo`, `detect`, `faces`, `pets`, `embeddings`, `thumbnails` |
  | L2 | application | `services`, `pipeline` |
  | L3 | delivery | `web`, `cli`, `desktop` |

  Adding a new top-level package under `trove/` and forgetting to
  place it in `LAYERS` fails the test outright rather than silently exempting
  the package from the rule.

## Where tests go

- `tests/unit/` — no database, no threads, no real files. Fast, pure-logic
  checks (filename date parsing, the Takeout sidecar matcher, the layering
  rule itself, and so on).
- `tests/integration/` — real SQLite and real files on disk (dedup grouping,
  merges, migrations, thumbnailing).
- `tests/gui/` — anything touching `JobManager`, the pipeline scheduler, or a
  live server (API routes, job logging, pipeline pause/resume).
- `tests/browser/` — the frontend, driven in a real headless Chrome. This is
  the only tier that executes the ES modules under `web/static/js/`, so it is
  where "the screen renders" and "navigating between screens raises nothing"
  are checked. **Not part of `make test`**: those tests start a browser, and
  the default suite must not depend on one. `make test-browser` runs them, and
  they skip themselves when no Chrome can be found, so a machine without one
  sees skips rather than failures.

All four tiers share `tests/factories.py` (`make_db`, `make_archive`,
`add_file`, `add_date`, `add_geo`, `add_person`, `add_face`, `add_pet`,
`add_place`, and friends) and `tests/helpers.py` (`serve_in_thread`,
`wait_until`) — read those before hand-rolling another way to build a fake
archive or spin up a test server. `tests/conftest.py` adds two fixtures used
throughout: `catalog` (an initialised database with one root, from
`factories.make_db`) and `source_root` (an empty fake archive root, fill it
via `factories.make_archive`). It also has an autouse fixture that points
`XDG_DATA_HOME` at a throwaway directory for every test, so nothing can
resolve to your real archive store by accident.

Markers are `slow` (measured over roughly a second — re-check with
`--durations` rather than guessing before adding one), `models` (needs
ONNX weights or a tokenizer checkpoint on disk), and `browser` (applied
automatically to everything under `tests/browser/`, never by hand).
`pyproject.toml` sets `--strict-markers`, so a typo'd marker is a collection
error, not a silent no-op.

Two shared skip marks live in `tests/helpers.py`, for the optional extras:
`needs_scoring` (numpy, enough to *score* an already-embedded query) and
`needs_embedding` (the whole SigLIP stack, needed to turn typed text into a
vector). Both mirror the app's own probes, so a test skips for the same reason
the feature would refuse. Prefer them on the individual test over a
module-level guard — most modules that need one are mostly about something
else, and skipping their neighbours hides tests that work fine without the
extra.

## How to look at a GUI change

Start with `make test-browser`: it drives the real frontend in a headless
Chrome it starts itself, and answers "does every screen still render, and does
moving between them raise anything". That is the automated half, and it is
where a new assertion about frontend *behaviour* belongs.

The rest of this section is the manual half — for looking at how a change
*appears*, which no assertion covers.

There is no selenium, playwright, or puppeteer here; all three of these drive
Chrome over the DevTools Protocol from the stdlib. `tools/dev/cdp_shot.py`
holds the client (`Tab`/`open_tab`) and a one-shot screenshot CLI on top of it;
`tools/dev/shoot_all.py` (imports `cdp_shot.py` by path) uses the same client
to shoot every route in both themes and diff two runs, and is what `make shots`
calls; `tests/browser/` asserts through it.

1. Start headless Chrome with a debugging port:

   ```
   chromium-browser --headless=new --disable-gpu --no-sandbox \
     --remote-debugging-port=9333 --remote-debugging-address=127.0.0.1 about:blank &
   ```

2. Run the GUI on a test port, never your real one. `make gui` does this
   correctly already — it sets `XDG_DATA_HOME` to a throwaway `.devdata/`
   directory under the repo before starting the server, because without that
   the GUI opens your real archive (hundreds of gigabytes) and its background
   pipeline auto-starts:

   ```
   make gui              # port 8799 by default; override with GUI_PORT=
   ```

3. Screenshot a route:

   ```
   .venv/bin/python tools/dev/cdp_shot.py \
     "http://127.0.0.1:8799/#/archive/<id>/<section>" out.png 3.5
   ```

   The wait argument (seconds, real wall-clock) has to cover whatever
   `fetch()` calls the route fires on load.

Three traps, each burned once already:

- **Do not** use plain `chromium --headless --screenshot=...` — it snapshots
  at the `load` event, before any async `fetch()` in the page resolves, so
  you get the empty shell, not the rendered screen.
- **Do not** use `--virtual-time-budget` — it looks like the fix and reliably
  hangs forever on any page with a running `setInterval`, which this GUI has
  (its job-status polling loop).
- **Close your tabs.** Both scripts open one tab and close it in a `finally`;
  if you drive the protocol by hand instead, close each tab yourself
  (`curl "http://127.0.0.1:9333/json/close/<id>"`). A handful of leftover
  tabs each keep polling the server in the background and are enough to
  starve the browser, making `Page.captureScreenshot` itself start timing out
  on later calls.

Kill your server and your headless Chrome when you're done.
