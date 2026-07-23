# Step 1 — Make application data install-safe

## Goal

Prepare `organize_archive` to run from an installed application bundle on Windows
and Linux.  The installed program itself must be treated as read-only; every piece
of mutable state must live in the current user's application-data directory.

This step deliberately does **not** add Electron/Tauri, redesign the interface, or
build installers.  It makes those later steps possible without losing a user's
catalogue when they update the app.

## Success criteria

- A normal launch never creates or changes `data/` beside installed source/code.
- Config, SQLite database, thumbnail cache, face-model cache, and logs (if added)
  live under an OS-appropriate per-user data directory.
- A fresh installation starts with no registered archive roots.
- Existing users can explicitly migrate their current project-local `data/`
  directory without modifying or deleting the old copy.
- `--db PATH` continues to override only the database path for isolated/testing
  use.
- Tests do not write to the real user profile.

## Target locations

Use a small, explicit path helper; avoid making the rest of the code assemble
platform paths itself.

| Platform | Default application-data directory |
| --- | --- |
| Linux | `$XDG_DATA_HOME/organize_archive`, otherwise `~/.local/share/organize_archive` |
| Windows | `%LOCALAPPDATA%\\organize_archive` |
| macOS (future-compatible) | `~/Library/Application Support/organize_archive` |

Within that directory, retain the current layout:

```text
organize_archive/
├── config.json
├── archive.db
├── cache/
│   ├── thumbs/
│   └── models/
└── logs/                 # reserve now; implementation optional in this step
```

Use the lowercase application identifier `organize_archive` consistently.  Do not
put user data under Electron's resources directory, a Python frozen-app directory,
or the repository root.

## Implementation plan

### 1. Centralize runtime paths

Create `organize_archive/paths.py` (or an equivalent narrowly scoped module) with:

- `app_data_dir() -> Path`
- `config_file() -> Path`
- `default_db_path() -> Path`
- `default_cache_dir() -> Path`
- `ensure_app_data_dirs() -> None`

Resolve environment variables at call time, not only during module import, so tests
can isolate them with `monkeypatch`.

Use only the Python standard library.  This keeps the application core dependency
free and avoids introducing `platformdirs` solely for three paths.

### 2. Change configuration defaults

Update `organize_archive/config.py` so its default paths come from the new helper.

- Replace `PROJECT_ROOT / "data"` defaults for DB, cache, and config.
- Change `DEFAULT_ROOTS` to an empty list.  The current `/media/capsa/...` default
  is development-machine-specific and must never reach an installer.
- Keep `PROJECT_ROOT` only for immutable development resources, such as an optional
  project-root `.env`; do not use it for runtime state.
- Ensure `Config.load()` creates no folders and is safe on first run.
- Ensure `Config.ensure_dirs()` creates the application-data directory, DB parent,
  cache, and any required cache subdirectories.
- Preserve unknown-key filtering when reading `config.json`.

### 3. Define first-run behavior

Do not silently initialize a catalogue or scan a location.

- A fresh `Config.load()` returns `roots=[]` and default DB/cache paths in the user
  data directory.
- `oa init` creates the database and writes configuration only when needed.
- Existing commands should give actionable messages when no database exists or no
  roots are configured.  For example: `No archive folders configured. Add one with
  oa config --add-root PATH.`
- Leave UI onboarding for Step 2; only ensure the backend can represent the
  first-run state cleanly.

### 4. Add a safe one-time migration command

Add a dedicated CLI command rather than auto-migrating during launch:

```text
oa migrate-data [--from PATH]
```

Default `--from` to the legacy repository-local `data/` directory when it exists;
otherwise require the argument.  The command must:

1. Validate that the source exists and contains one or more recognized artefacts
   (`config.json`, `archive.db`, or `cache/`).
2. Refuse to run if the target application-data directory already contains an
   existing catalogue/config, unless an explicit future `--merge` policy is
   designed.  Do not invent merge behavior.
3. Copy files into the new location using `shutil.copy2` / `copytree` semantics;
   never move or delete the source.
4. Print both source and destination paths and state that the original was kept.
5. Exit non-zero for invalid sources or conflicting targets.

For this first implementation, copying is safer than clever partial migration.

### 5. Make bundled resources explicit

The following are application resources, not user data:

- `organize_archive/gui/index.html`
- `organize_archive/gui/vendor/*`
- `organize_archive/db/schema.sql`

Confirm they continue to be read relative to their installed Python package path.
Update `pyproject.toml` package-data declarations to include `gui/vendor/*` and
image assets (`*.png`) as well as `index.html` and `*.sql`.  This avoids a frozen
application opening with missing map icons or styles later.

### 6. Add tests

Add focused tests using a temporary directory and patched environment variables.

- Linux XDG path selection, including the fallback when `XDG_DATA_HOME` is absent.
- Windows `LOCALAPPDATA` path selection (mock `sys.platform`).
- First-run config has empty roots and creates no files merely by loading.
- `ensure_dirs()` creates directories under the mocked app-data path.
- Legacy migration copies config/database/cache and leaves the source intact.
- Migration rejects an occupied target and an unrecognized source.

Avoid assertions tied to the actual developer machine or its media archive.

## Suggested implementation sequence

1. Add path-helper tests, then implement `paths.py`.
2. Switch `Config` to the helper and update affected current tests/commands.
3. Implement and test `oa migrate-data`.
4. Update package-data declarations.
5. Run the full test suite and manually verify a clean first run with a temporary
   app-data directory.

## Verification checklist

```bash
pytest -q

# Linux/manual isolation example
XDG_DATA_HOME="$(mktemp -d)" .venv/bin/python -m organize_archive config --show
XDG_DATA_HOME="$(mktemp -d)" .venv/bin/python -m organize_archive init
```

Confirm that the first command prints empty `roots`, and that the second writes only
inside the temporary XDG directory.  Also run `oa migrate-data --from ./data` against
a disposable temporary target and verify `./data` is unchanged.

## Deliberately out of scope

- Electron/Tauri project files and desktop-window behavior.
- Native folder-picking and UI onboarding.
- Installer generation, code signing, auto-update, and bundled external binaries.
- Visual redesign.
- Automatic migration or destructive cleanup of legacy data.

## Handoff to Step 2

Once this is complete, report the changed files, final CLI behavior, and any design
choices that differ from this plan.  Step 2 can then design the first-run and archive
selection experience around the finalized data/configuration API.
