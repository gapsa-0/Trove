# One place to learn this repo. `make` on its own prints the list.
#
# CI runs `make lint` and `make test` rather than its own inlined commands, so
# the two cannot drift apart and "green locally, red in CI" stays abnormal.

.DEFAULT_GOAL := help
.PHONY: help setup lint lint-py lint-js fmt test test-fast gui shots api-docs check

# `?=` so CI can point this at the interpreter it already installed into:
# the runner has no .venv, and sets PY=python in the job environment.
PY ?= .venv/bin/python
# The interpreter used to *create* the venv. Overridable, because a system
# python3.13 is not universal yet: `make setup PYTHON=/path/to/python3.13`.
PYTHON ?= python3.13
EXTRAS := dev,cli,media,faces,pets,semantic
GUI_PORT ?= 8799

help:            ## Show this help
	@grep -hE '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/'

setup:           ## Create the venv and install everything for development
	$(PYTHON) -m venv .venv
	$(PY) -m pip install -U pip
	$(PY) -m pip install -e '.[$(EXTRAS)]' -c constraints.txt
	$(PY) -m pre_commit install
	cd desktop && npm ci

# Split because CI runs the two halves in different jobs: the Python job has no
# node and the electron job has no Python. Developers want both, so `lint` is
# still the one-word answer.
lint: lint-py lint-js  ## Static checks (fast — run this before every commit)

lint-py:         ## Python static checks only (what CI's python job runs)
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

lint-js:         ## JavaScript static checks only (what CI's electron job runs)
	cd desktop && npm run lint

fmt:             ## Autoformat
	$(PY) -m ruff format .
	$(PY) -m ruff check --fix .

test:            ## Full test suite
	$(PY) -m pytest -q

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
	XDG_DATA_HOME=$(CURDIR)/.devdata $(PY) -m organize_archive gui --port $(GUI_PORT)

# Needs `make gui` running in another shell, plus a headless Chrome with a
# debugging port open (tools/dev/shoot_all.py's docstring has the command).
shots:           ## Screenshot every route into shots/ as a refactor guardrail
	$(PY) tools/dev/shoot_all.py http://127.0.0.1:$(GUI_PORT) shots/ 1

# Run this after adding a route. CI runs the same script with --check, so a
# route added without regenerating the doc fails the build rather than shipping
# a document that quietly no longer describes the server.
api-docs:        ## Regenerate docs/dev/api.md from the route tables
	$(PY) tools/dev/gen_api_docs.py

check: lint test ## Everything CI runs
