# 0001. One SQLite database and cache per archive

- **Status:** Accepted
- **Date:** 2026-08-01

## Context

The GUI lets a user add more than one archive folder — a separate family
member's Takeout export, a different drive, a one-off event folder. Early on,
all of that lived in a single shared catalogue (`Config.db_path` /
`Config.cache_dir`), the way the CLI still works today. That design meant one
corrupt or oversized database affected every archive at once, and removing an
archive required picking its rows back out of tables that also held everyone
else's.

## Decision

Each archive the GUI knows about gets its own directory,
`archives/<id>/`, under the application-data folder, holding its own
`archive.db` and its own thumbnail and face-crop cache. `organize_archive/config/archives.py`'s
`ArchiveRegistryMixin` is what allocates and looks after these ids:
`allocate_archive_id` claims the next free one and creates the directory
before anything is registered, and `_next_archive_id` deliberately unions the
registry with whatever directories already exist under `archives/`, so a
removed archive's id is never handed out again — reusing it would point a
fresh archive at a stale database whose `roots` row still names the old
folder.

The registry id is used directly as `roots.id` inside that archive's own
database — the two numbering schemes are kept identical on purpose so there
is never a mapping from "GUI archive id" to "row id inside the database" to
get wrong or out of sync. `organize_archive/pipeline/archives.py` (which
answers per-archive questions like disk-file counts and whether a dedup
rebuild is owed) and the rest of the pipeline address an archive purely by
this one id, via `cfg.archive_db_path(archive_id)` /
`cfg.archive_cache_dir(archive_id)`.

Only two things are shared across every archive: the downloaded
machine-learning model weights and the app icon, both because they are
identical regardless of which archive is open, and both large enough that
duplicating them per archive would be wasteful. `Config.db_path` and
`Config.cache_dir` (the pre-existing fields) still point at these shared
resources — see the comment on `Config` in
`organize_archive/config/settings.py` for the split.

Legacy installs that predate per-archive isolation are handled by
`migrate_legacy_archive`: it copies the old shared database into the new
per-archive layout the first time it runs (latched by `Config.legacy_migrated`
so it happens at most once), and reconciles the copy's root id to match the
newly allocated archive id via `db.reconcile_root`. A legacy database that
already held more than one root cannot be split apart automatically — its
rows may mix data across roots in ways per-archive isolation can no longer
represent (cross-root duplicate groups, for instance) — so migration is
skipped for that case and the folders have to be re-added as separate
archives by hand.

**The CLI is deliberately not per-archive.** `oa scan`, `oa dedup`, and the
rest of the command-line tool still use one shared catalogue at `cfg.db_path`
— it is a separate, unchanged tool from the GUI's notion of an archive, and
nothing about this decision touches it.

## Consequences

- An archive can be removed by deleting its `archives/<id>/` directory; no
  other archive's data is touched.
- A corrupt or oversized catalogue is contained to the one archive that owns
  it.
- Only the GUI benefits from this isolation; the CLI's mental model (one
  shared database across every configured root) is unchanged and the two are
  not interchangeable.
- The README's "Data locations and backups" section documents the resulting
  layout (`archives/<id>/`, shared `config.json` / `secrets.json` / models)
  and this ADR is consistent with it.
