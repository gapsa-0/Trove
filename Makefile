# One place to learn this repo. `make` on its own prints the list.
#
# CI runs `make lint` and `make test` rather than its own inlined commands, so
# the two cannot drift apart and "green locally, red in CI" stays abnormal.

.DEFAULT_GOAL := help
.PHONY: help setup lint lint-py lint-js handlers sizes fmt test test-fast gui shots api-docs check

# `?=` so CI can point this at the interpreter it already installed into:
# the runner has no .venv, and sets PY=python in the job environment.
PY ?= .venv/bin/python
# The interpreter used to *create* the venv. Overridable, because a system
# python3.13 is not universal yet: `make setup PYTHON=/path/to/python3.13`.
PYTHON ?= python3.13
EXTRAS := dev,cli,watch,media,faces,pets,semantic,documents,ocr
GUI_PORT ?= 8799

help:            ## Show this help
	@grep -hE '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/'

setup:           ## Create the venv and install everything for development
	$(PYTHON) -m venv .venv
	$(PY) -m pip install -U pip
	$(PY) -m pip install -e '.[$(EXTRAS)]' -c constraints.txt
	$(PY) -m pre_commit install
	cd desktop && npm ci
	@# npm ci can report success and still leave no Electron binary: the
	@# postinstall that unpacks it exits 0 without unpacking on an unsupported
	@# Node (docs/adr/0014). desktop/.npmrc already refuses that Node outright;
	@# this catches any future install script that fails the same silent way,
	@# because a setup that "succeeded" is the part that cost days.
	@test -f desktop/node_modules/electron/path.txt || { \
	  echo "setup failed: desktop/node_modules/electron has no unpacked binary."; \
	  echo "npm ci exited 0 without installing it. Select the Node in .nvmrc"; \
	  echo "(fnm use, or nvm use), then re-run make setup."; \
	  exit 1; }
	@# Same hint npm ci already printed, repeated as the last thing setup says.
	@# npm prints its own summary after a lifecycle script, so on a distribution
	@# that needs the setuid sandbox the note scrolls past under the deprecation
	@# warnings; this is where it is still on screen when setup finishes.
	@cd desktop && node scripts/check-sandbox.cjs

# Split because CI runs the two halves in different jobs: the Python job has no
# node and the electron job has no Python. Developers want both, so `lint` is
# still the one-word answer.
lint: lint-py lint-js  ## Static checks (fast — run this before every commit)

lint-py:         ## Python static checks only (what CI's python job runs)
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .
	$(PY) -m mypy trove

lint-js:         ## JavaScript static checks only (what CI's electron job runs)
	cd desktop && npm run lint

# Not lint: it compares two files that no linter reads together. An inline
# handler naming a function main.js does not export renders perfectly and does
# nothing when clicked, so this is the only automated check that catches it.
handlers:        ## Check every inline on* handler resolves to main.js's export block
	$(PY) tools/dev/check_handlers.py

# A file or function that grows past budget still passes every other check --
# ruff and mypy grade style and correctness, not shape. This is what keeps a
# "just one more case" habit from quietly turning one module into the place
# every future change lands.
sizes:           ## Check tracked files and functions against a shrink-only size ratchet
	$(PY) tools/dev/check_sizes.py

fmt:             ## Autoformat
	$(PY) -m ruff format .
	$(PY) -m ruff check --fix .

# The browser tier is deselected here rather than skipped inside itself: it
# starts a real Chrome, and `make test` should not quietly depend on one being
# installed. It skips itself too when none can be found, so the CI step that
# does run it degrades to a skip rather than a failure on a runner without one.
test:            ## Full test suite (without the browser tier -- see test-browser)
	$(PY) -m pytest -q -m "not browser"

# Starts its own headless Chrome per session, or attaches to one you already
# have with TROVE_CDP_PORT=9333. Skips itself if neither is available.
test-browser:    ## Browser tier: drive the real frontend in headless Chrome
	$(PY) -m pytest -q -m browser tests/browser

# The per-save loop, and it only earns that name at ~2s. Naming the tier is not
# redundant with -m "not slow": deselecting the slow marks alone still leaves
# ~21s, because the suite's cost is not a few sleepers but ~300 tests each
# paying to create a fresh SQLite schema. -m still matters here -- it drops the
# 13s SigLIP module, which lives in the unit tier.
test-fast:       ## Unit tier only, skipping the slow ones (the per-save loop)
	$(PY) -m pytest -q -m "not slow" tests/unit

# XDG_DATA_HOME is not optional here. Without it the GUI opens the *real*
# archive in the user's home data dir -- 500 GB of family photos -- and its
# pipeline auto-starts, so a casual `make gui` would kick off face detection and
# semantic indexing over the whole catalogue. .devdata/ is gitignored and empty
# until you register an archive in it.
gui:             ## Run the GUI on a test port against a throwaway dev data dir
	XDG_DATA_HOME=$(CURDIR)/.devdata $(PY) -m trove gui --port $(GUI_PORT)

# Needs `make gui` running in another shell, plus a headless Chrome with a
# debugging port open (tools/dev/shoot_all.py's docstring has the command).
shots:           ## Screenshot every route into shots/ as a refactor guardrail
	$(PY) tools/dev/shoot_all.py http://127.0.0.1:$(GUI_PORT) shots/ 1

# Run this after adding a route. CI runs the same script with --check, so a
# route added without regenerating the doc fails the build rather than shipping
# a document that quietly no longer describes the server.
api-docs:        ## Regenerate docs/dev/api.md from the route tables
	$(PY) tools/dev/gen_api_docs.py

check: lint handlers sizes test test-browser ## Everything CI runs

# `build/lib/` is a setuptools staging directory that is never cleaned between
# builds, so a `pip wheel` in a working copy copies the current tree in beside
# whatever was there before and ships both. A local wheel has already been
# observed carrying a package that had been renamed two commits earlier. This
# is release step 6 (docs/release.md) as a command rather than a paragraph
# someone has to remember.
clean:           ## Remove build artifacts that would otherwise be shipped stale
	rm -rf build/ dist/ *.egg-info
