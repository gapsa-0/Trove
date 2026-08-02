# Architecture

## What this is

Trove is a read-only catalogue for a large, chaotic media collection: photos,
videos, audio, and documents spread across one or more folders, often
including Google Takeout exports. It scans a folder in place, builds a SQLite
database describing what it found, and offers navigation by date, place,
person, pet, and duplicate group on top of that database — without ever
touching the originals.

Two constraints explain almost every design choice in the codebase:

- **Originals are read-only.** The app never moves, renames, edits, or
  deletes a file under a source root. Every derived fact — a resolved date, a
  thumbnail, a face embedding, a cluster membership — lives in the SQLite
  database or a separate cache directory, never next to the original.
- **Nothing leaves the machine.** Scanning, hashing, metadata extraction,
  deduplication, place clustering, face/pet detection and clustering, and
  semantic search all run locally. There is one bounded exception: the map's
  street-map tile layer, a user-facing toggle (default on), which sends photo
  *coordinates* to a public tile server — never a photo. Local models
  (detection, embedding, search) download their weights once on first use and
  run offline after that.

## Data flow

Hashing happens inside the scan stage, not as a separate one: `scan` walks the
source root(s), fast-hashes and SHA-256s each new or changed file, and writes
the `files` row. `enrich` runs in parallel with it, resolving date/GPS/Takeout
metadata for whatever `scan` has already committed. Once both have caught up,
`dedup` rebuilds duplicate groups wholesale; three independent stages then
fan out from it. `detect` finds faces and pets in a single decode pass per
image/frame and immediately clusters them into people and pets. This is the
real dependency graph, from `organize_archive/pipeline/stages.py`:

```text
  scan ────┐                  ┌──▶ detect ──▶ cluster ──▶ persons / pets
  (walk +  │                  │    (faces + pets, one decode pass;
   hash)   ├──▶ dedup ────────┤     re-clusters after every chunk)
           │                  │
  enrich ──┘                  ├──▶ places ────────────▶ place_clusters
  (exif, Takeout sidecar,     │    (GPS clustering)
   filename parse, mtime)     │
                              └──▶ semantic ──────────▶ embeddings
                                   (optional, SigLIP 2)

                   one archive.db per open archive (SQLite)
                                    ▲ read / write
                   services/   business rules, own connection per call
                                    ▲
                   web/        localhost-only HTTP API
                                    ▲
                   static frontend  plain JS/CSS, no build step
```

`README.md` carries a shorter version of the same graph (scan/metadata →
dedup → {people & pets, places, semantic}); this is that graph with hashing,
the metadata sub-sources, and clustering made explicit — the two are not in
tension.

Long stages commit progress in batches and are resumable: interrupting a scan
or a detect run loses at most the current batch, and a re-run picks up from
the catalogue rather than starting over. The scheduler and the GUI both read
stage status from the *same* resolved function (`stage_states` in
`pipeline/stages.py`), so a status card can never disagree with what the
pipeline is actually doing.

## The layer map

The package is enforced into four layers by
`tests/unit/test_layering.py`: a module may import its own layer or any layer
below it, never above, and this is checked by walking the whole AST — a
deferred (function-local) import counts as much as a top-level one. Its
`ALLOWED_VIOLATIONS` list is empty by policy: exceptions get fixed, not
grandfathered.

| Layer | Packages | Role |
| --- | --- | --- |
| L0 foundation | `config`, `paths`, `runtime`, `logging_setup`, `errors`, `db` | Knows nothing about the rest of the package: settings, filesystem locations, process wiring, and the SQLite connection/schema layer. |
| L1 domain | `scan`, `hashing`, `metadata`, `media`, `dedup`, `geo`, `detect`, `faces`, `pets`, `embeddings`, `thumbnails` | The archive's actual algorithms — one package per concern, each usable on its own against a connection it is handed. |
| L2 application | `services`, `pipeline` | Orchestrates L1: `services/` holds the business rules a caller invokes (merges, renames, browse queries); `pipeline/` schedules and runs the stages above. |
| L3 delivery | `web`, `cli`, `desktop`, `__main__` | Translates an external request (HTTP, command line, desktop shell) into one service call and serialises the result. Holds no business logic itself. |

## Where do I put this?

| I want to change… | Go to |
| --- | --- |
| how a date is chosen | `organize_archive/metadata/resolver.py` |
| how Takeout sidecars are matched | `organize_archive/metadata/takeout.py` |
| what the Library grid shows | `organize_archive/services/browse.py` + `organize_archive/web/static/js/library.js` |
| when a pipeline stage runs | `organize_archive/pipeline/stages.py` |
| how a job does its work | `organize_archive/pipeline/runners/<kind>.py` (e.g. `scan.py`, `enrich.py`, `dedup.py`, `detect.py`, `face_cluster.py`, `pet_cluster.py`, `places.py`, `semantic.py`) |
| a new API endpoint | `organize_archive/web/routes/<domain>.py`, then add it to the route tables in `organize_archive/web/routes/__init__.py` — see below |
| what the detectors find, and photo orientation | `organize_archive/detect/extract.py` |
| how detections are stored (and what survives a re-detect) | `organize_archive/detect/persist.py` |
| face clustering behaviour | `organize_archive/faces/cluster.py` |
| pet clustering behaviour | `organize_archive/pets/cluster.py` |
| how a screen looks | `organize_archive/web/static/css/<area>.css` (e.g. `library.css`, `people.css`, `map.css`) |
| the SQLite schema | `organize_archive/db/schema.sql`, plus the migration in `init_db` (`organize_archive/db/database.py`) |
| settings and their defaults | `organize_archive/config/settings.py` |
| where things live on disk | `organize_archive/paths.py` |

For a new API endpoint: `GET_ROUTES` / `POST_ROUTES` in
`organize_archive/web/routes/__init__.py` map an exact path to a handler, and
`GET_PREFIX_ROUTES` holds the parameterised ones (`/thumb/<id>`,
`/api/faces/person/<id>`, …), checked exact-before-prefix. Together these
three tables are the single source of truth for what the server answers.
`docs/dev/api.md` is generated from them by `tools/dev/gen_api_docs.py` and
CI fails if it has drifted, so a new route needs the table entry — the docs
follow automatically.

## The schema, summarised

`organize_archive/db/schema.sql` currently defines 31 tables, grouped by what
they describe:

| Group | Tables |
| --- | --- |
| Catalogue | `roots`, `files`, `media_meta`, `dates`, `geo`, `takeout_sidecar`, `orientation` |
| Dedup | `dup_groups`, `dup_members`, `perceptual_hashes`, `dedup_runs` |
| Places | `place_clusters`, `place_cluster_members`, `place_merges` |
| People | `persons`, `faces`, `face_links`, `person_merges`, `person_files`, `fiqa_calibration` |
| Pets | `pets`, `animal_detections`, `pet_links`, `pet_merges`, `pet_files`, `nonhuman_detections` |
| Semantic | `semantic_embeddings` |
| Bookkeeping | `app_state`, `scan_runs`, `face_scan`, `pet_scan` |

`orientation` is grouped with the catalogue rather than with people or pets:
it records a per-file pixel-rotation correction (detected from face/pet
evidence but applied to the image as a whole) and every face/pet box for that
file is already expressed in its rotated frame. `fiqa_calibration` holds the
population statistics (mean/std of the AdaFace feature norm) that map a
face's raw quality score onto its HIGH/BORDERLINE/LOW_QUALITY tier, so it
travels with `faces`. `nonhuman_detections` is where a face-like region
suppressed by an overlapping animal/toy box goes for review — reachable from
both People and Pets, filed here with pets because that is the detector that
vetoed it.

`SCHEMA_VERSION` (currently 13) lives in `organize_archive/db/database.py`.
`init_db` is close to additive-only: on every run it creates any missing
table/index (`CREATE ... IF NOT EXISTS`) and adds any missing column
(`_add_column_if_missing`), never drops or renames one. The one exception is
a single gated `DELETE` that ran once, for databases upgrading from before
version 12, to clear out `semantic_embeddings` rows written by a
now-abandoned whole-video embedding scheme; it is keyed off the database's
own previous `user_version` so it cannot fire twice.

## Non-obvious invariants

- Person and pet ids are destroyed and rebuilt on every clustering pass
  (`faces/cluster.py`, `pets/cluster.py` DELETE and recreate `persons` /
  `pets` wholesale). Manual tags therefore anchor to a person/pet **name**,
  not an id, and a repair step re-points them onto whichever id currently
  carries that name after each rebuild
  (`organize_archive/faces/manual_tags.py`,
  `organize_archive/pets/manual_tags.py`).
- **Re-detecting a file rewrites all of its detections wholesale**, and one
  thing must survive that. `detect/persist.py` deletes the file's faces,
  animals, suppressed candidates and orientation before writing what the pass
  found, which is what makes a detector or config change take full effect
  instead of layering new rows on stale ones. But the animal-overlap veto is
  re-run each time, so a face a *user* has already reviewed as human would be
  suppressed again: their answer is carried across the rewrite (keyed on the
  box, since the row id does not survive) and the face they restored is
  re-created. A veto with no record is a veto nobody can appeal — which is
  exactly what shipped between `67a2c5c` and `e9a8391`, with the Pets review
  queue silently unfillable for nine days.
- `config.json` persists most `Config` fields (`organize_archive/config/settings.py`),
  so changing a dataclass default does nothing on an existing install —
  retuning a threshold there means editing that install's `config.json`.
- Pipeline status is in-memory by design (`organize_archive/pipeline/stages.py`
  module docstring): it is derived fresh from the catalogue and the live job
  list each time, not persisted, because the pipeline runs once per session
  and then goes idle.
- Analytics count every present file; only Browse hides non-canonical
  duplicates — `services/browse.py`'s module docstring calls this out
  explicitly, working in `_NOT_HIDDEN` terms while the dashboard counts
  everything present.
- A Takeout `geoData`/`geoDataExif` of `0.0/0.0/0.0` means *no location* and
  is stored as NULL, never as a real coordinate at the equator.
- Each open archive has its own `archive.db` and thumbnail/face-crop cache
  under `archives/<id>/` in the app's data directory
  (`organize_archive/paths.py`); only downloaded model weights and the app
  icon are shared across archives.
- A `services/` function takes a `db_path` and opens its own SQLite
  connection per call, rather than being handed a shared one — stated as a
  contract in `organize_archive/services/__init__.py`'s module docstring.
  This is deliberate, not wasteful: handlers run concurrently under
  `ThreadingHTTPServer`, and a `sqlite3` connection may not be shared between
  threads, so a fresh connection per call is what keeps the layer safe to
  call from anywhere. The same module states the layer's other contract:
  writes (merges, renames, date overrides) live in `services/`, with their
  transaction boundaries owned there, not in the HTTP handler.
