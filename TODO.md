# TODO / Roadmap — organize_archive

Legend: `[ ]` todo · `[~]` in progress · `[x]` done

Guiding constraints (see CLAUDE.md): originals are **read-only**, everything is
**local**, all long operations are **resumable & incremental**, every derived fact
carries **provenance**.

---

## Phase 0 — Project setup
- [ ] Initialize git repository.
- [ ] `pyproject.toml` + virtualenv; pin dependencies.
- [ ] `config.py`: source roots, cache dir, DB path, ignore list, date-priority config.
- [ ] Preflight check for `exiftool` and `ffmpeg`/`ffprobe` with clear error messages.
- [ ] Basic CLI skeleton (`typer` + `rich`), logging.

## Phase 1 — Database core
- [ ] SQLite schema + migrations (`files`, `media_meta`, `dates`, `geo`, `phash`,
      `dup_groups`, `dup_members`, `takeout_sidecar`, `roots`).
- [ ] DB access/query layer (wrapped so storage is swappable later).
- [ ] Provenance columns on every derived field.

## Phase 2 — Scan & index  *(MVP)*
- [ ] Resumable directory walker with ignore rules (json-as-content, db, thm, ini,
      nomedia, part, tmp, index files).
- [ ] Change detection: skip unchanged files by path + size + mtime.
- [ ] Fast partial hash (size + head/tail) as prefilter, then full SHA-256.
- [ ] Media-type classification (image / video / audio / document / other).
- [x] `scan` and `status` CLI commands; progress + interruption safety.

## Phase 3 — Metadata & dates  *(MVP)*
- [x] EXIF/embedded metadata reader (via exiftool): dimensions, camera, duration.
- [x] Content-based type detection (exiftool/magic) for extensionless files —
      e.g. Google Motion Photos `MVIMG_*` and truncated Takeout names currently
      fall into media_type "other".
- [x] **Google Takeout sidecar matcher** — robust to Google's quirks:
      base `.json`, `.supplemental-metadata.json`, ~46-char truncation,
      `(1)` counter placement, `-edited` variants. Log unmatched pairs.
- [x] Takeout JSON parser: `photoTakenTime` (date), `geoData`/`geoDataExif`
      (`0.0/0.0` → NULL), `description`.
- [x] Multi-format filename date parser (`IMG_20220514_090957`, `IMG-...-WA0001`,
      `2022-05-14`, `20220514`, …).
- [x] Date resolver with **configurable** priority
      (Takeout JSON → EXIF → filename → mtime); store source + confidence.
- [x] GPS resolver with provenance.
- [x] Unit tests using real weird filenames/sidecars from the archive.

## Phase 4 — Deduplication  *(MVP)*
- [ ] Exact-duplicate grouping via SHA-256.
- [ ] Perceptual hash for images (`imagehash`/pHash); near-duplicate grouping with
      configurable threshold.
- [ ] Deterministic canonical selection (resolution → size/least recompression →
      richer metadata → earliest date → stable path).
- [ ] Mark non-canonical members hidden (never deleted); `dups list` / `dups show`.
- [ ] (Optional, later) generate a user-run cleanup/hardlink **plan** — tool never deletes.

## Phase 5 — Navigation & query  *(MVP)*
- [ ] Query by date (year / month / range).
- [ ] Query by media type.
- [ ] Query by source folder.
- [ ] Combined filters; thumbnail generation into the cache dir.

---
## Later phases (ambitious features)

## Phase 6 — Face recognition (local)
- [ ] Local detection + embeddings (`insightface`/`face_recognition`, `onnxruntime`).
- [ ] Face clustering into persons; label persons; query "photos of X (and Y)".
- [ ] Extend to video (sampled frames).

## Phase 7 — Map
- [ ] Aggregate GPS points; export/visualize photo & video locations on a map.

## Phase 8 — AI descriptions & semantic search (local)
- [ ] Local image embeddings (CLIP/sentence-transformers) + `sqlite-vec` vector search.
- [ ] Auto captions/descriptions; free-text content search.

## Phase 9 — Timeline & pets
- [ ] Timeline visualization across the archive.
- [ ] Pet detection.

## Phase 10 — GUI
- [ ] Visual browser over the database (dedup review, date/type/folder/person/map).

---
## Known edge cases / watch-list
- Multiple takeouts from different family members → heavy cross-source duplication.
- Google filename truncation & counter-placement in sidecar matching.
- `0.0/0.0/0.0` geo = "no location", not the equator.
- HEIC / live-photo pairs; `-edited` derivatives.
- `.opus` = WhatsApp voice notes; `.thm`/`.db`/`.ini`/`.nomedia` = junk.
- Timezones: Takeout timestamps are UTC; decide on display timezone.
