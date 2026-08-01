# 0009. The product is called Trove; the package, CLI, and data directory are not

- **Status:** Accepted
- **Date:** 2026-08-01

## Context

The product was renamed to Trove (the README's opening line: "Trove is a
desktop catalogue for a large, messy media collection..."). A rename touches
more than marketing copy if it also changes identifiers that persist state on
disk: the Python package name, the command-line entry point, the desktop
application id, and the application-data directory all have existing
installs depending on their current values.

## Decision

The rename is deliberately user-visible only — documentation, the installer's
product name, and the desktop shell's own dialogs — and deliberately does
**not** touch the underlying identifiers:

- The Python package is still `organize_archive`
  (`organize_archive/__init__.py` and everywhere it is imported), and the
  distribution name in `pyproject.toml` is `name = "organize-archive"`.
- The CLI entry point is still `oa` — `pyproject.toml`'s
  `[project.scripts]` maps `oa = "organize_archive.cli:main"`.
- The desktop application id is still `io.capsa.organize-archive`
  (`desktop/electron-builder.yml`'s `appId`).
- The application-data directory is still named `organize_archive`, not
  `Trove` — confirmed in `organize_archive/paths.py`'s use of
  `app_data_dir()` and in the README's "Data locations and backups" table
  (`$XDG_DATA_HOME/organize_archive` on Linux, `%LOCALAPPDATA%\organize_archive`
  on Windows, `~/Library/Application Support/organize_archive` on macOS).

The README states the reasoning for this directly, in the closing paragraph
of "Data locations and backups": "The directory is still named
`organize_archive` rather than `Trove`. That is deliberate: the product name
changed, but the package, CLI, application id and data path did not, so
catalogues built by earlier versions keep working." This ADR's own reading
of the code — the package, entry point, and app id above — is consistent
with that paragraph and adds no correction to it.

**"Archive" survives as domain vocabulary, in two overlapping senses.** The
word names both "an archive folder you added" (the everyday sense the README
and GUI use — "Adds one or more archive folders and keeps a separate
catalogue for each one") and, unrelated to that, appears throughout the code
as the technical unit of per-archive isolation described in ADR 0001
(`archives/<id>/`, `archive_db_path`, `archive_cache_dir`, `ArchiveRegistryMixin`).
Both senses are intentional and both are "archive" — the word is not a
naming clash to be resolved, it is the same underlying concept (one added
folder's isolated catalogue) described once for the end user and once for
the code that implements it.

## Consequences

- An install upgraded from a pre-Trove version keeps its existing catalogue,
  config, cache, and CLI invocations working unchanged — nothing about the
  rename requires a migration.
- Anyone reading the codebase or an install's file paths sees `organize_archive`
  throughout, not `Trove`; only user-facing text (README, installer,
  desktop dialogs) shows the new name. This is expected, not an
  inconsistency to be cleaned up.
- Any future documentation or code that introduces a *new* identifier should
  keep this split in mind — user-facing strings say Trove, persisted
  identifiers and package/module names stay `organize_archive`/`oa`.
