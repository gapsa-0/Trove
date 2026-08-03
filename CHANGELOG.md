# Changelog

All notable user-facing changes to Trove are documented in this file.

Trove ships as a desktop app with an installer, not a library people pull with a
package manager — for someone on an older version deciding whether to download a
newer one, this file (and the GitHub release notes generated from it) is the only
account of what changed. Internal restructuring, typing and lint work is summarised
under `Internal` rather than itemised: it has no user-visible effect and belongs in
`ARCHITECTURE.md` for contributors, not here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Semantic (description) search now runs entirely on-device on SigLIP 2, replacing
  the Voyage cloud API. Search queries and photo contents no longer leave the
  machine at all; the API-key setup step and its "no key configured" state are
  gone.
- Each pipeline stage in Library health can now be paused and resumed on its own,
  instead of only the whole pipeline at once. A stage paused this way stays paused
  across a restart.
- The map has a Places/Photos switch: alongside the existing grouped-place view,
  it can plot one dot per geotagged photo, coloured by the place it belongs to, for
  seeing the actual spread of a place rather than just where you keep going back.
- The Duplicates page breaks the redundant-copies total down by what the copies
  actually are: byte-identical versus only visually the same, and photos versus
  videos.
- `oa logs` prints the application log, or `oa logs --path` its location, so a
  bug report can carry the evidence needed to act on it.

### Changed

- **The installers are roughly half the size.** Three things were shipping weight
  nobody could use: the People and Pets model weights travelled inside the download
  (349 MB) even though the app already knows how to fetch weights on demand; FFmpeg
  was bundled as two self-contained binaries that each carried a complete copy of
  the codec set; and the OpenCV build shipped a full Qt desktop toolkit for an app
  that never opens a window. The weights are now downloaded on first use like every
  other model, so the first People/Pets run fetches about 550 MB instead of 220 MB
  and needs a working connection once. Nothing else about the app changes.
- Model downloads now report progress. A first run that fetches several hundred
  megabytes used to show one unchanging line on the stage card and read as hung.
- The Overview storage panel was reworked: one bar with a Size/Files switch
  instead of two competing bars, exact numbers moved into the table, and per-type
  share shown on hover.
- Merging two people of the same size, neither of them named, now keeps the older
  of the two rather than depending on which card you dragged onto which — the rule
  pets and places already followed.

### Fixed

- People & pets detection no longer stops with "neither the face nor the pet models
  could be loaded" when the app is run from a source checkout. Two of its four model
  files had no way to be downloaded outside a packaged build, so the stage fetched
  ~310 MB of the other two and then failed; they are fetched and hash-verified like
  every other weight now. When a model genuinely cannot be obtained, the stage says
  so before downloading anything, and the card names the cause instead of pointing
  at messages the user cannot see.
- The Semantic indexing card reports its model download instead of sitting blank
  through it. The progress callback was captured by whichever part of the app
  loaded the model first, which was always the silent start-up warm-up. That
  warm-up no longer downloads anything either: it warms weights that are already on
  disk, so a 317 MB download can never start unannounced in the background.
- Faces that detection discards as an animal's own can be reviewed again. Since the
  people-and-pets detectors were merged into one pass, every such face was dropped
  with no record, so the "not an animal" review list on the Pets screen was always
  empty and a person mistaken for a pet could not be recovered. Correcting one now
  also survives a re-scan.
- Closing the app no longer hangs for several seconds when detection or semantic
  indexing has just started. Those stages spend their first seconds loading a
  model, which cannot be interrupted, so shutdown no longer waits on them.
- Opening the map on a freshly scanned archive could fail with a database error
  while a scan was running, because the first view clustered the places itself.
  Clustering now belongs solely to the pipeline stage; until it runs, the map
  shows the photos as ungrouped dots.
- `oa scan` crashed immediately with `UnboundLocalError` before doing any work,
  on every invocation. Five other commands carried the same redundant import but
  happened to survive it; all six are cleaned up.

### Internal

The package was reorganised into layers (services, a route table for the HTTP
API, a pipeline package for background jobs), the frontend was split out of one
large `index.html` into ES modules with their own stylesheets, and mypy and ruff
gates were added to CI along with a faster, better-organised test suite. A size
ratchet now fails the build when a file or function grows past budget, and the
repository carries the documents a newcomer expects: architecture, contributing,
security, and decision records.

The Node version used to build the desktop app is now pinned and enforced. On
newer versions Electron's install step exited successfully without unpacking the
runtime, so a source checkout produced an app that failed at launch while every
check stayed green; `npm ci` now refuses an unsupported Node, and both `make
setup` and CI verify the binary is actually there.

## [0.1.2] - 2026-07-29

### Changed

- The Pets grids now update in place while detection runs, instead of rebuilding
  the whole section on every change. Scroll position and an in-progress review no
  longer get reset out from under you.

### Fixed

- Console windows no longer flash repeatedly during video processing on Windows.
  The desktop build owns no console of its own, so Windows opened and closed one
  for every spawned `ffmpeg`/`exiftool` call — tens of thousands of flashes over a
  full pipeline run on a large archive.

## [0.1.1] - 2026-07-28

### Added

- Places can now be merged by dragging one onto another, the same way People and
  Pets already could.
- The Library grid can be sorted oldest-first as well as newest-first, and now
  shows which search produced the results on screen, with a way to clear it.

### Changed

- The sidebar pipeline status gives each running stage its own readable progress
  row (label, percentage, and a progress bar) instead of overlapping spinners and
  bare percentages.
- Face clustering now grades every detection for quality and keeps low-quality
  faces (blurry, tiny, badly framed) out of the clustering process entirely,
  instead of only using that signal advisorially. This prevents a handful of weak
  detections from bridging two different people into one cluster.

### Fixed

- Fixed a bug where an archive's catalogue could be filed under the wrong internal
  id: on the affected install this hid over 99% of a 97,000-file archive, kept the
  Overview numbers from ever moving, and made the scan stage rescan the entire
  archive over and over without making progress.
- A fully-scanned archive is no longer rescanned repeatedly; scan completion is
  now recorded explicitly instead of inferred from a count that could never
  reliably reach zero.
- The People grid now updates in place instead of rebuilding itself on every
  detection poll, which used to drop loaded pages, reset scroll position, and
  interrupt an in-progress "Same person?" review.

## [0.1.0] - 2026-07-26

First public release. Trove catalogues a large, messy media collection —
photos, videos, audio, documents, phone dumps and Google Takeout exports —
without ever moving, renaming, or modifying an original file. At this release
it could: scan incrementally and resumably; read dates and GPS from Takeout
sidecars, embedded metadata, filenames and file timestamps, picking a best
date by a documented priority order; find byte-identical and visually similar
duplicate copies and pick a canonical one; build a timeline, library, folder
view and item inspector; cluster geotagged media into named places; detect and
cluster faces into people and detect cats, dogs, birds and horses into pets,
including inside video keyframes; merge, unmerge, and manually tag people and
pets; search the library by description when semantic indexing was enabled;
and correct photo orientation for display without touching the file on disk.

[Unreleased]: https://github.com/gapsa-0/Trove/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/gapsa-0/Trove/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/gapsa-0/Trove/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/gapsa-0/Trove/releases/tag/v0.1.0
