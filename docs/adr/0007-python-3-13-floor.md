# 0007. The Python floor is 3.13

- **Status:** Accepted
- **Date:** 2026-08-01

## Context

The project could in principle support an older Python to widen who can build
it from source. The desktop app, however, ships its own bundled interpreter —
an end user never has their own Python version in play at all — so lowering
the floor would only benefit people building from a checkout: the maintainer,
and any future contributor.

## Decision

`pyproject.toml` declares `requires-python = ">=3.13"`. The same version is
pinned consistently across the toolchain rather than left to drift:
`mypy`'s `python_version = "3.13"`, `ruff`'s `target-version = "py313"` (with
the `UP` pyupgrade rule set enabled specifically to modernise to 3.13 idioms,
per its own comment), and CI's `actions/setup-python@v5` step in
`.github/workflows/ci.yml` pinned to `python-version: '3.13'`. There is no
compatibility matrix — CI runs one version, and no code path is audited
against 3.12 or earlier.

The decision was to stay at this floor rather than lower it for broader
source-build compatibility, because the actual install path for a user is
the packaged desktop build (Windows, Linux AppImage, Debian/Ubuntu — see the
README's "Install" table), which bundles "its own Python runtime, FFmpeg,
and the local detection models" (README, "Install"). Nothing about the
version installed on the end user's machine — if they have Python at all —
affects the shipped app. The floor is therefore a decision about the
developer experience of building from source (`make setup`, which the README
notes falls back to `make setup PYTHON=/path/to/python3.13` if the system's
default `python3` is older), not about end-user compatibility.

## Consequences

- A contributor building from source needs Python 3.13 on their machine (or
  to point `make setup` at one); there is no fallback path for an older
  interpreter.
- CI gives no signal about compatibility with any other Python version —
  raising or lowering the floor in the future would need its own
  verification, not just a version-string edit.
- End users of the packaged builds are entirely unaffected by this choice,
  since they never run the project's own Python at all.
