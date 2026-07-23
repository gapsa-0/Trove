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
- [x] Local detection + embeddings — OpenCV YuNet detector + SFace 128-d
      embeddings (`faces/backend.py`); weights fetched once into the cache dir,
      no image ever leaves the machine. Behind an interface so a higher-accuracy
      backend can be swapped in.
- [x] Resumable/incremental extraction (`faces/extract.py`, `face_scan` marker).
- [x] Auto-queued in the background pipeline (scan → dates → **faces** → dedup),
      default-on and pausable from Overview; extracts in chunks and re-clusters
      after each so the Faces section fills in live.
- [ ] Efficiency: run faces *after* exact-dedup and skip `hidden` byte-identical
      copies, so the same face isn't detected once per takeout duplicate.
- [x] Face clustering into persons — **two-stage, chaining-resistant**
      (`faces/cluster.py`): tight over-cluster (blocked-cosine + union-find) →
      complete-linkage centroid merge, on non-hidden faces only. Replaced plain
      DBSCAN, which chained ~55% of faces into one junk cluster; now the biggest
      cluster is a coherent ~8% (verified by intra-cluster tightness ~0.75).
      Idempotent rebuild, user names preserved by member overlap.
- [ ] Reduce the ~1800-cluster long tail: add an extraction-time, configurable
      face **quality gate** (focus/blur metric on the aligned crop, detector
      confidence, and minimum original-pixel size) before embedding or writing a
      `faces` row. Calibrate on a small sample and apply to future scans only;
      no live-database migration is required. Also add a **merge/split people** UI.
      Lower `faces_link_sim` and/or the ArcFace upgrade would assign more of the
      ~half-of-faces currently left unassigned (mostly genuine one-off faces).
- [ ] Keep People human-only: reject doll, cartoon, and other non-human face
      detections at extraction time with a human-vs-nonhuman classifier (rather
      than relying on YuNet confidence). `not_person` remains only a manual
      correction path for existing rows.
- [x] `oa faces` CLI + GUI Faces section: person cards, per-person photos,
      rename, recompute. Query "photos of X".
- [x] Fixed a face-crop coordinate bug: Pillow's `Image.draft()` (used to speed
      up decoding large JPEGs before detection) silently shrinks `im.size` as a
      side effect, which the old scale-back-to-original math didn't account
      for — crops landed on the wrong, wrong-scale region of the photo for any
      image large enough to trigger it. Fixed in `faces/backend.py`; existing
      DB rows repaired once via `tools/fix_face_boxes.py` (header-only, no
      re-detection needed — embeddings/clustering were never affected).
- [x] Faces person view: sticky topbar (back button + name) so navigating back
      to the people grid doesn't require scrolling up first.
- [ ] Higher-accuracy **insightface / ArcFace** backend behind `faces/backend.py`
      (the `ai` extra), + an ANN index (e.g. `sqlite-vec`/hnsw) so clustering
      and "find this face" scale past ~100k faces without O(N²).
- [ ] Multi-face query "photos of X **and** Y"; merge/split people; hide a person.
- [ ] Extend detection to video (sampled frames).
- [x] **In-detail face relabelling** — the media detail panel shows a square crop of
      every detected face with a person dropdown; reassigning pins the face to that
      named person (`faces.manual_person`, by name) and re-applies it after every
      automatic recluster (`faces/cluster.py` `_apply_manual_pins`/`_finalize`), so the
      correction is never wiped. Renames keep pins in sync.

## Phase 7 — Map / Places
- [x] Aggregate GPS points; visualize photo & video locations on an interactive map.
- [x] **Durable place entities** — places are no longer rebuilt from scratch each run.
      `geo/clusters.py` bootstraps once, then `assign_unplaced` adds new geotagged files
      incrementally (attach to nearest place ≤300 m, else spawn one) and never deletes a
      place or member. Users can attach any file (even GPS-less) to a place as a manual
      member (membership only — no coordinates written), create a new place by dropping a
      map pin, and edit a file's date (variable precision: year / year-month / full day)
      — all from the detail panel.
- [ ] Reverse-geocode centroids into place names; browse the dedicated Places section.

## Phase 8 — AI descriptions & semantic search (local)
- [ ] Local image embeddings (CLIP/sentence-transformers) + `sqlite-vec` vector search.
- [ ] Auto captions/descriptions; free-text content search.

## Phase 9 — Timeline & pets
- [ ] Timeline visualization across the archive.
- [ ] Dedicated **Pets** pipeline and GUI section: use an animal/pet detector or
      classifier (not the human-face detector) to find and group cats/dogs and
      other pets. Keep its detections and identities separate from `faces` /
      `persons`; doll/cartoon false positives are discarded, not treated as pets.

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
