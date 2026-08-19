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
  (detection, embedding, search) download their weights once, as soon as an
  archive that asked for them is created, and run offline after that.

## Data flow

Hashing happens inside the scan stage, not as a separate one: `scan` walks the
source root(s), fast-hashes and SHA-256s each new or changed file, and writes
the `files` row. `enrich` runs in parallel with it, resolving date/GPS/Takeout
metadata for whatever `scan` has already committed. Once both have caught up,
`dedup` rebuilds duplicate groups wholesale — but not the near-duplicate search
behind them, which is cached per file against the content it was run for (ADR
[0022](docs/adr/0022-cached-near-duplicate-pairs.md)); three independent stages
then fan out from it. `detect` finds faces and pets in a single decode pass per
image/frame and immediately clusters them into people and pets. This is the
real dependency graph, from `trove/pipeline/stages.py`:

```text
  scan ────┐                  ┌──▶ places ────────────▶ place_clusters
  (walk +  │                  │    (GPS clustering)
   hash)   ├──▶ dedup ────────┤
           │                  ├──▶ detect ──▶ cluster ──▶ persons / pets
  enrich ──┘                  │    (faces + pets, one decode pass;
  (exif, Takeout sidecar,     │     re-clusters after every chunk)
   filename parse, mtime)     │
                              ├──▶ semantic ──────────▶ embeddings
                              │    (optional, SigLIP 2)
                              │
                              └──▶ text ──────────────▶ doc_text / doc_chunks
                                   (optional; documents + text in
                                    images, one open per file, and
                                    a PDF routed per page)

                   one archive.db per open archive (SQLite)
                                    ▲ read / write
                   services/   business rules, own connection per call
                                    ▲
                   web/        localhost-only HTTP API
                                    ▲
                   static frontend  plain JS/CSS, no build step
```

`README.md` carries a shorter version of the same graph (scan/metadata →
dedup → {places, people & pets, semantic}); this is that graph with hashing,
the metadata sub-sources, and clustering made explicit — the two are not in
tension.

Long stages commit progress in batches and are resumable: interrupting a scan
or a detect run loses at most the current batch, and a re-run picks up from
the catalogue rather than starting over. The scheduler and the GUI both read
stage status from the *same* resolved function (`stage_states` in
`pipeline/stages.py`), so a status card can never disagree with what the
pipeline is actually doing. `pipeline/status.py` sits on top of that with the
part only the GUI needs — the words, the pause overlay, one overall verdict —
and decides nothing the scheduler could disagree with.

## The layer map

The package is enforced into four layers by
`tests/unit/test_layering.py`: a module may import its own layer or any layer
below it, never above, and this is checked by walking the whole AST — a
deferred (function-local) import counts as much as a top-level one. Its
`ALLOWED_VIOLATIONS` list is empty by policy: exceptions get fixed, not
grandfathered.

| Layer | Packages | Role |
| --- | --- | --- |
| L0 foundation | `config`, `paths`, `app_data_migration`, `runtime`, `model_manifest`, `features`, `logging_setup`, `errors`, `progress`, `db` | Knows nothing about the rest of the package: settings, filesystem locations, process wiring, the catalogue of what an archive can be asked to do, the shape a long pass reports progress through, and the SQLite connection/schema layer. |
| L1 domain | `scan`, `hashing`, `metadata`, `media`, `dedup`, `geo`, `detect`, `faces`, `pets`, `embeddings`, `text`, `thumbnails` | The archive's actual algorithms — one package per concern, each usable on its own against a connection it is handed. |
| L2 application | `services`, `pipeline` | Orchestrates L1: `services/` holds the business rules a caller invokes (merges, renames, browse queries); `pipeline/` schedules and runs the stages above. |
| L3 delivery | `web`, `cli`, `desktop`, `__main__` | Translates an external request (HTTP, command line, desktop shell) into one service call and serialises the result. Holds no business logic itself. |

## Where do I put this?

| I want to change… | Go to |
| --- | --- |
| how a date is chosen | `trove/metadata/resolver.py` |
| how Takeout sidecars are matched | `trove/metadata/takeout.py` |
| what the Library grid shows | `trove/services/browse.py` + `trove/web/static/js/library.js` |
| when a pipeline stage runs | `trove/pipeline/stages.py` |
| what an archive can be asked to do, and the words describing it | `trove/features.py` (see ADR 0015) |
| which model weights a feature needs, and when they are downloaded | `trove/services/models.py` + `trove/pipeline/runners/models.py` |
| how the archive setup screen looks and behaves (creating one) | `trove/web/static/js/setup.js` + `web/static/css/setup.css` |
| how a live archive's features are changed (the Features sheet) | `trove/web/static/js/features.js` + `web/static/css/features.css` |
| the rules a feature set obeys, shared by both of those | `trove/web/static/js/feature-rules.js` |
| what a status card *says* (its wording, its bar, the pause overlay) | `trove/pipeline/status.py` |
| how a job does its work | `trove/pipeline/runners/<kind>.py` (e.g. `scan.py`, `enrich.py`, `dedup.py`, `detect.py`, `face_cluster.py`, `pet_cluster.py`, `places.py`, `semantic.py`, `text.py`) |
| a new API endpoint | `trove/web/routes/<domain>.py`, then add it to the route tables in `trove/web/routes/__init__.py` — see below |
| what the detectors find in one frame (and the people-vs-pets cross-check) | `trove/detect/frame.py` |
| which way up a photo really is | `trove/detect/orientation.py` |
| how a video is sampled, and repeats across its frames collapsed | `trove/detect/video.py` |
| when the detect stage runs a file, and in what batches | `trove/detect/extract.py` |
| how detections are stored (and what survives a re-detect) | `trove/detect/persist.py` |
| face clustering behaviour (the two passes and their thresholds) | `trove/faces/passes.py` |
| what a face re-cluster destroys, and what survives it | `trove/faces/cluster.py` |
| pet clustering behaviour | `trove/pets/cluster.py` |
| which semantic matches are shown (the two cuts, and the modality-gap centering they are tuned for) | `trove/services/search.py`, with the thresholds and their reasoning in `trove/config/settings.py` |
| how a file's text is read, and what a format contributes | `trove/text/extract.py`, then the reader for that family (`pdf.py`, `office.py`, `plain.py`, `ocr.py`) |
| whether a PDF page is read as text or as pixels | `trove/text/pdf.py`'s `page_stats` / `looks_scanned`, thresholds in `trove/config/settings.py` (ADR 0019) |
| what OCR costs, and why it reads at two resolutions | `trove/text/ocr.py` (its docstring carries the measurements) |
| how a document is cut into searchable passages | `trove/text/chunk.py` (its docstring carries the token measurements the sizes came from) |
| when the text stage re-reads a file | `trove/services/documents.py`'s four-legged pending predicate, and `TEXT_VERSION` beside it |
| how text search ranks, and what a hit shows | `trove/services/text_search.py` + the text group in `trove/web/static/js/library.js`, whose tiles are in `tiles.js` |
| how the two document rankings are fused, and where each is cut | `trove/services/text_search.py`'s `_rrf` / `_vector_ranked`, with the thresholds in `trove/config/settings.py` (ADR 0018) |
| the text embedder's recipe, and why there are two embedders | `trove/embeddings/text_backend.py` |
| how a screen looks | `trove/web/static/css/<area>.css` (e.g. `library.css` for Browse's controls, `results.css` for what a query returns, `people.css`, `map.css`) |
| which control to reach for when adding one | `trove/web/static/css/theme.css` — `.btn`/`.btn.sec` for an action being asked for, `.quietbtn` (`.sm` beside a heading or in a row) for one being offered, `backControl()` in `static/js/router.js` for a way back (ADR 0023) |
| the SQLite schema | `trove/db/schema.sql`, plus the migration in `init_db` (`trove/db/database.py`) |
| settings and their defaults | `trove/config/settings.py` |
| where things live on disk | `trove/paths.py` |
| how a pre-rename install's data folder is carried across | `trove/app_data_migration.py` (see ADR 0016) |

For a new API endpoint: `GET_ROUTES` / `POST_ROUTES` in
`trove/web/routes/__init__.py` map an exact path to a handler, and
`GET_PREFIX_ROUTES` holds the parameterised ones (`/thumb/<id>`,
`/api/faces/person/<id>`, …), checked exact-before-prefix. Together these
three tables are the single source of truth for what the server answers.
`docs/dev/api.md` is generated from them by `tools/dev/gen_api_docs.py` and
CI fails if it has drifted, so a new route needs the table entry — the docs
follow automatically.

## The schema, summarised

`trove/db/schema.sql` currently defines 34 tables, grouped by what
they describe:

| Group | Tables |
| --- | --- |
| Catalogue | `roots`, `files`, `media_meta`, `dates`, `geo`, `takeout_sidecar`, `orientation` |
| Dedup | `dup_groups`, `dup_members`, `perceptual_hashes`, `dup_edges`, `dup_edge_scan`, `dedup_runs` |
| Places | `place_clusters`, `place_cluster_members`, `place_merges` |
| People | `persons`, `faces`, `face_links`, `person_merges`, `person_files`, `fiqa_calibration` |
| Pets | `pets`, `animal_detections`, `pet_links`, `pet_merges`, `pet_files`, `nonhuman_detections` |
| Semantic | `semantic_embeddings` |
| Document text | `doc_text`, `doc_chunks` (+ `doc_chunk_fts`, see below) |
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

`SCHEMA_VERSION` (currently 14) lives in `trove/db/database.py`, and the
migrations it runs live beside it in `trove/db/migrations.py`.

`doc_chunk_fts` is the one table `schema.sql` does not declare. It is an FTS5
virtual table, and `executescript` runs that file at every job start on every
archive -- so an unsupported statement there would fail archives that never
asked to read a document. `migrations._migrate_text_index` creates it where
FTS5 exists and leaves it absent where it does not, and the text features report
themselves unavailable in that case (ADR 0017).
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
  (`trove/faces/manual_tags.py`,
  `trove/pets/manual_tags.py`).
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
- `config.json` persists most `Config` fields (`trove/config/settings.py`),
  so changing a dataclass default does nothing on an existing install —
  retuning a threshold there means editing that install's `config.json`.
- Pipeline status is in-memory by design (`trove/pipeline/stages.py`
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
  (`trove/paths.py`); only downloaded model weights and the app
  icon are shared across archives.
- **An archive runs only the features it was set up with, and the gate is one
  omission** (ADR 0015): `stage_states` leaves a disabled stage out of the list
  it returns, so the scheduler never starts it, its weights are never fetched
  (both the stage and the fetch job that gets them ahead of it read the same
  enabled feature set), and `cards()` builds no card for it. There is no
  "disabled" state to render anywhere. An archive registered before this
  existed has no `features` key and gets the full set, so an upgrade never
  switches off work already in progress.
- **A feature is named and marked once, in `features.py`, for every surface
  that shows it.** The setup card, the pipeline chip, the Features sheet card, the
  Overview health card and the sidebar status line all resolve through
  `card_label`, `card_running`
  and `card_icon`; none of them keeps a wording or an icon of its own. Three
  such tables used to live in `pipeline/stages.py` and the frontend, and all
  three named the same five things differently from the panel that offered
  them. A card whose feature is only partly enabled is composed from the half
  that is on, which is why the shared People & pets card can say "Finding
  pets…" — see `tests/unit/test_features.py`, which also pins the invariants
  the composition rests on (features sharing a card share a verb; the panel's
  chain order is the Overview's card order).
- **The Overview's rail and the setup panel's chain draw one graph.** The card
  payload carries `always_runs` (`features.card_always_runs`) so the rail can
  mark the trunk without the frontend knowing which features are required. Two
  invariants in `tests/unit/test_features.py` keep the drawing honest: the
  trunk is exactly the cards a minimum archive still runs, and no optional
  stage depends on another optional stage — otherwise a branch would need to
  fork off a branch, which neither screen draws.
- **A feature that unlocks no section is gated where it lives.** Hiding a nav
  section is how most features disappear, and Search by description has none —
  it is the composer at the top of Browse. So `library.js` gates on the feature
  (`archiveHasFeature`) what the composer's words are *matched against*, and
  `/api/browse/semantic/status` reports `configured` only when the archive both
  chose the feature and can run it. Reporting importability alone was the bug:
  an archive that declined the feature was offered a search over an index
  nothing would ever write. The box itself is ungated, because its floor asks
  nothing of any stage: with no search feature the words are matched against
  file names, which the scan already recorded (`browse.media`'s `name`).
- A stage may only depend on a stage owned by a *required* feature. Otherwise a
  stage whose dependency was switched off would sit blocked on a state that can
  never arrive — `tests/unit/test_features.py` enforces this rather than
  leaving it to memory.
- The fused detect pass is told which detectors it was asked for, and touches
  nothing belonging to the other one: no scan marker (or the backlog would
  never settle once that feature came back) and no row deletion (or switching
  Pets off would destroy every animal already found). See `detect/persist.py`.
- A `services/` function takes a `db_path` and opens its own SQLite
  connection per call, rather than being handed a shared one — stated as a
  contract in `trove/services/__init__.py`'s module docstring.
  This is deliberate, not wasteful: handlers run concurrently under
  `ThreadingHTTPServer`, and a `sqlite3` connection may not be shared between
  threads, so a fresh connection per call is what keeps the layer safe to
  call from anywhere. The same module states the layer's other contract:
  writes (merges, renames, date overrides) live in `services/`, with their
  transaction boundaries owned there, not in the HTTP handler.
