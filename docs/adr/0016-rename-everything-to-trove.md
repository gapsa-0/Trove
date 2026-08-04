# 0016. The package, CLI, application id and data directory are all Trove too

- **Status:** Accepted
- **Date:** 2026-08-04
- **Supersedes:** [0009](0009-product-name-and-package-name.md)

## Context

ADR 0009 kept the rename to Trove user-visible only. The package stayed
`organize_archive`, the distribution `organize-archive`, the CLI entry point
`oa`, the desktop application id `io.capsa.organize-archive`, and the
application-data directory `organize_archive`. The reasoning was compatibility:
those identifiers persist state on disk or in muscle memory, and leaving them
alone meant an upgraded install kept working with no migration to write.

The cost of that split is that the codebase answers to two names. Every import,
every path in `pyproject.toml`'s per-file lint and type configuration, the
PyInstaller spec, the log-level environment variable `OA_LOG_LEVEL`, and the
`docs/` prose all read `organize_archive`, while everything a user sees reads
Trove. ADR 0009 anticipated this and accepted it — "expected, not an
inconsistency to be cleaned up". What it did not weigh is that the split has to
be re-explained to every reader indefinitely, and that its own closing rule
("any future documentation or code that introduces a *new* identifier should
keep this split in mind") makes the two-name state permanent rather than
transitional.

The compatibility argument was also narrower than it looked. Only one of the
four identifiers actually holds user data: the application-data directory. The
desktop application id does not determine where the desktop shell stores
anything — Electron derives `userData` from `productName`, which has been
`Trove` since the original rename. The package and distribution names are
install-time concerns that `pip install -e .` resolves. The CLI name is
re-typed, not migrated.

## Decision

Everything is called Trove. `organize_archive/` is now `trove/`, the
distribution is `trove`, the CLI entry point is `trove` (`oa` is gone, not
aliased), the backend script is `trove-backend`, the application id is
`io.capsa.trove`, the log-level variable is `TROVE_LOG_LEVEL`, and
`paths.APP_NAME` — the application-data directory — is `trove`.

The one identifier that holds user data gets a migration rather than a break.
`trove/app_data_migration.py` runs on startup from both entry points
(`cli.main` and `desktop.main`), before `Config.load()`, and moves a directory
left by a pre-rename install to the new name. Three things about it are load-bearing:

- **It repoints `config.json`.** `Config.save()` persists `db_path` and
  `cache_dir` as absolute strings, so moving the directory alone would leave a
  populated install pointing at a path that no longer exists — presenting a
  full catalogue as an empty one, which is the failure most easily mistaken for
  data loss. Only values that resolved *inside* the old directory are rewritten;
  a database someone deliberately put on another volume is left alone.
- **It never merges.** If both directories hold data, neither is touched and
  the collision is logged.
- **`logs/` does not count as data.** `logging_setup` creates it on the first
  record, which happens before the migration runs, so counting it would make
  every launch look like a collision and strand the migration permanently.

It moves rather than copies, so it costs nothing on a large catalogue, and the
common case — a fresh install, or the second launch of a migrated one — returns
at the first check.

**"Archive" still survives as domain vocabulary**, exactly as ADR 0009
described: an "archive folder you added" in the GUI, and `archives/<id>/`,
`archive_db_path` and `ArchiveRegistryMixin` in the code (ADR 0001). That word
was never the product name and is unaffected by any of this.

## Consequences

- An install predating the rename is migrated on first launch with no user
  action and no re-scan. The migration is covered by
  `tests/unit/test_app_data_migration.py`, including the collision, the
  unreadable-config, and the run-it-twice cases.
- `oa` no longer exists. Scripts, aliases and shell history that call it break
  with "command not found" and must be updated to `trove`. This is the one
  deliberate hard break; it was chosen over an alias so there is one name to
  document rather than two to explain.
- `OA_LOG_LEVEL` is likewise gone in favour of `TROVE_LOG_LEVEL`.
- The application id changed, so a Windows NSIS install of a pre-rename build
  is a *different application* to the installer, not an upgrade of it. Both can
  sit in Add/Remove Programs until the old one is uninstalled. The catalogue is
  unaffected — it is keyed by the data directory, which is migrated.
- `migrate_legacy_app_data()` is dead weight once no pre-rename install remains
  in the wild. It is one L0 module with no dependencies beyond `paths`, and
  deleting it is a self-contained change whenever that point is judged to have
  arrived.
- The rule ADR 0009 left behind — new identifiers keep the split — is withdrawn.
  New identifiers are named Trove.
