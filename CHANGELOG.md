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

- **Trove can read the writing in your pictures.** Switch on *Search by picture
  text* and photographed receipts, screenshots and scanned paperwork become as
  searchable as anything else. A PDF is decided page by page, so a contract with
  a scanned appendix is read both ways and comes back as one document, with the
  page each passage came from. Spanish and English, accents included.

  Nothing downloads: this is the only model that ships inside the application,
  so it starts work straight away even with no connection.

  **It is the slow one, and worth deciding about rather than switching on by
  reflex.** Every picture has to be opened and looked at — it cannot use the
  small thumbnails, because writing disappears at that size — which is roughly
  half a second each. Five thousand pictures is under an hour; a hundred
  thousand is an overnight run. It runs alongside everything else, never blocks
  browsing, and stops and resumes safely, so leaving it overnight is the
  intended way to use it. A result read from a picture is marked as such,
  because unlike a document's own text it is a best guess.
- **Trove can read what is inside your documents.** Switch on *Search by document
  text* when you set up an archive, and the search box looks inside the files rather
  than only at their names: type a phrase from a contract and the contract comes
  back, showing the passage that matched and the page it was on. It reads PDFs
  that carry a text layer, Word, Excel and PowerPoint files, OpenDocument files,
  plain text, Markdown, CSV, web pages and notebooks — spreadsheet numbers
  included, since an invoice total is exactly the sort of thing worth searching
  for. Accents do not have to match (`peticion` finds `petición`), and a plural
  finds its singular.

  Nothing is downloaded for it and nothing leaves the machine: reading a
  document is parsing rather than recognition, so there is no model involved. On
  a few thousand files it is minutes, and it runs alongside everything else.

  Two limits. A scanned PDF is a picture of a page with no text in the file at
  all, so this finds nothing in one — the feature's card says so before you
  switch it on, that is what *Search by picture text* is for, and switching it on
  later re-reads every file this feature had to pass over. And pre-2007 `.doc`,
  `.xls` and `.ppt` files cannot be read at all; they are listed and dated like
  any other file and reported as an unsupported format, rather than half-read
  into something misleading.
- **Browse says what it can search, before you ask it to.** With the box empty
  there is now a panel under it listing every way this archive can answer a
  query, what each one matches in plain words, and how much of the archive it
  currently covers — so you can see that 300 documents have been read and 6,000
  photos are still queued. Each way links to the page documenting it, and the
  one fed by two or three features links to each of them. An archive with no
  search features switched on gets the panel too, saying it has one way.

  Every way is named after the feature you switched on to get it, so a group of
  results is headed with the same words as the card you chose it from, the card
  reporting its progress and the page explaining it — *Search by document or
  picture text*, *Search by description*. The one way no feature produces, searching
  filenames, is named to read alongside them.

  When you search, those same rows become the headings over their own results,
  so what the screen promised and what it labels are the same list. A way that
  found nothing says so on one quiet line at the foot rather than leaving you to
  wonder whether it ran.
- **Every archive can be searched by file name, and now it always is.** Matching
  what you type against the names of the files themselves needs no model, no
  download and no indexing, so it runs on every search whatever else is switched
  on, and gets a group of its own with the matched part of the name marked.
  Every word has to appear in the name, in any order, so `escritura 2019` finds
  `2019-escritura-casa.pdf`, and only the file's own name counts — a folder
  called `playa` does not make everything inside it a match for "playa".

  Until now it was a *fallback*, reached only by archives with no other way to
  search, which meant switching on Search by description quietly took it away:
  `IMG_2019` went to the picture model, scored below its relevance floor, and
  came back with nothing at all. Searching by description no longer costs you
  searching by name.
- **A search result says how it was found.** Where a group could have found
  something two ways, each result now carries it: text read from a file's own
  words is told apart from text read off the pixels of a scan or a screenshot,
  because the second is a best guess where the first is what the file says.
- **Browse captions each thumbnail with the file's name** rather than its date.
  The grid is already broken into dated sections, so the date under every tile
  was repeating the heading above it, while the name is the one thing that says
  which file you are looking at. The grids that have no such headings — a
  person's photos, a pet's, a place's — still caption with the date.
- **Each archive now chooses what Trove does with it.** Adding a folder opens a
  setup screen: name the archive, and build up what it runs by dragging features
  onto it — People, Pets, Places, Search by description — or leave them off.
  Indexing and duplicates always run, because everything else reads what they
  produce. A feature you do not choose is not merely hidden: its stage never
  runs, and its models are never downloaded, so an archive that only wants
  duplicates found no longer waits on a 689 MB download for a search it will
  never use. Each feature says up front what it does and what it costs, and
  a feature whose models are already on this machine from another archive says
  so rather than quoting a download that will not happen.
- Features can be changed later from the same screen — "Set up" on any archive
  on the start page. Adding one picks up from where the catalogue already is;
  removing one deletes nothing, so putting it back does not start over.
- People and Pets can now be chosen independently. They still share a single
  pass over each photo, so having both costs barely more than having one.

- Semantic (description) search now runs entirely on-device on SigLIP 2, replacing
  the Voyage cloud API. Search queries and photo contents no longer leave the
  machine at all; the API-key setup step and its "no key configured" state are
  gone.
- Each pipeline stage in Library health can now be paused and resumed on its own,
  instead of only the whole pipeline at once. A stage paused this way stays paused
  across a restart — for that archive alone.
- The map has a Places/Photos switch: alongside the existing grouped-place view,
  it can plot one dot per geotagged photo, coloured by the place it belongs to, for
  seeing the actual spread of a place rather than just where you keep going back.
- **Open a file and you can see its copies.** The viewer's panel has a
  Duplicates section showing the whole group the file belongs to — every copy as
  a thumbnail, which one Trove keeps, which are byte-identical and which only
  look the same, and which one you are looking at. Pressing another copy opens
  it, with the arrows kept inside the group and a way back to where you started.
  A file that has been compared and matched nothing says so; one the last
  grouping run has not reached yet says that instead, rather than claiming it has
  no copies. This replaces a line in Details reading "3 copies", which told you
  three files somewhere were the same and left you to go and find them.
- The Duplicates page breaks the redundant-copies total down by what the copies
  actually are: byte-identical versus only visually the same, and photos versus
  videos.
- A duplicate group's copies now wrap onto as many lines as they need instead of
  scrolling sideways, so a group of twenty can actually be compared against
  itself.
- The Duplicates page can be narrowed to groups holding an identical copy or a
  visual match, and ordered by how many copies a group holds as well as by how
  much space it would give back.
- The Duplicates page leads with how many unique files the archive holds — every
  file, with each set of copies counted once — and says how much of it is still
  waiting to be compared, the same way People and Pets report their own progress.
  An archive with nothing grouped yet keeps those numbers on screen instead of
  replacing the page with an empty-state box.
- `trove logs` prints the application log, or `trove logs --path` its location, so a
  bug report can carry the evidence needed to act on it.

### Fixed

- **Animated pictures are no longer grouped as copies of each other.** A GIF was
  compared on its first frame alone, so two unrelated animations that open the
  same way — a shared title card, a fade in from white — were treated as one
  picture and one of them was hidden from your library. Animations are now
  matched on their contents like videos are, and this is decided by looking
  inside the file, so a GIF saved as a `.png` is handled correctly too. Anything
  wrongly hidden comes back at the next automatic rebuild.
- **Thumbnails no longer come up blank until you force a reload.** A thumbnail
  is generated the first time it is asked for, and a second request arriving
  while the first was still writing it was answered with the half-written file —
  which the browser then remembered as a broken picture. Grids of copies were
  worst hit, since identical files share one thumbnail and ask for it at the same
  moment. Every generated thumbnail, video frame and face crop is now published
  in one step, so it is either absent or whole and never something in between.
- **A photograph found by its writing no longer swallows the results beside
  it.** Text-search results hold two kinds of file — documents read from their
  own text, and pictures read off the pixels — and a picture's thumbnail had
  nothing bounding its height there. It grew to fill the column, stretched the
  whole row to match, and left every document next to it in a cell four times
  the height of the single line of text inside it. Every result in that group
  now shows itself at the same size, whichever kind of file it is, and its name
  sits above the passage rather than under a shadow meant for laying text over a
  photo.
- **An archive that only reads the writing in pictures can now search it.**
  Switching on *Search by picture text* without its document half filled the index
  exactly as it should, and Browse never showed the group that searches it — so the work
  was done, the passages were there, and there was nowhere to look. The same
  archive was also told it had nothing to read and nothing queued, because
  coverage counted documents rather than the files its readers actually open.
  Both halves write into one index, so either one alone now brings the group and
  the count with it.
- **Browse's "How this works" opened the page about scanning folders**, which
  answered a question nobody pressing it had. It now opens a page about
  searching — and both text features have one to link to from the setup screen,
  which they did not before, so choosing them was the one decision
  made with no way to read what it does first.
- **Browse opens on a finished-looking screen, and opens far faster.** It used
  to wait for the filter bar's options — which years, which people, which
  places this archive holds — before doing anything else, and that answer is a
  pass over every file in the catalogue. So the screen you got first was a
  heading, an empty toolbar row with a stray sort box adrift at its end, and no
  media at all; on a large archive just after opening it, that sat there for
  several seconds, because nothing it needed was in memory yet and the pipeline
  was busy with the same disk. Now the media grid loads at the same time rather
  than behind it, the sort control arrives filled in, and the filters stand in
  for themselves at their settled size until they land — nothing on the screen
  moves when they do. Coming back to Browse reuses the options it already
  fetched, so the bar is simply there. The queries behind all of this were
  narrowed too: on a 97,000-file archive with nothing cached, the filter
  options went from 2.4s to 0.2s and the first page of media from 0.74s to
  0.35s. Existing archives pick that up the next time they are opened.

- **A paused scan no longer looks like it threw away its progress.** Resuming
  sent the bar back to 0 and raced it up to where it stopped, which read as the
  whole scan being redone. Nothing was: scanning restarts at the top of the
  folder rather than at its own backlog, and a file it already has costs a
  quick check and no re-reading, so it crosses that ground very fast. It now
  says what it is doing — "Re-checking 12,400 files already scanned…" — and the
  bar reappears at the point the interrupted run reached. Dating files had the
  same problem from the other end: its bar measured the work that was *left*,
  so each run restarted at 0% of a total that had shrunk to match. Both now
  measure against the whole archive, the way finding people and indexing for
  search already did.

- An archive set up **without** Search by description was still shown the
  description-search box at the top of its browse screen, above a line
  promising files "queued for indexing". Nothing was queued and nothing ever
  would be — declining the feature leaves its indexing stage out of the
  pipeline entirely — so the search could only ever come back empty. The box
  and that line now appear only on archives that run the feature; the grid,
  filters and sorting are unaffected.

### Changed

- **The two text features are named after what you can search, not the files they
  read.** *Documents* and *Pictures of text* are now **Search by document text**
  and **Search by picture text**, headed as *Search by document or picture text*
  where both are on. The old names described their input, which read as a promise
  about a kind of file rather than a way of finding one — and *Documents* was
  actively misleading, since a scanned contract is a document and is precisely
  what that half cannot read. Both now sit in one grammar with *Search by
  description* and *Search by filename*: every way of searching is named for what
  you type against. Nothing about what they do has changed, and an archive already
  running them keeps everything it has read.
- **The command is now `trove`, not `oa`.** The rename to Trove previously
  stopped at what you could see; the package, the command and the data folder
  were all still called `organize_archive` underneath. They are not any more —
  there is one name for all of it. Your catalogue moves with it: the first time
  this version starts it relocates the old data folder and repoints what is
  recorded inside it, so nothing is re-scanned and nothing is lost. If you have
  scripts, aliases or shell shortcuts that call `oa`, they need updating to
  `trove` — the old command is gone rather than kept as an alias, so they will
  fail loudly rather than quietly doing something else. `OA_LOG_LEVEL` is now
  `TROVE_LOG_LEVEL` for the same reason. On Windows, because the application's
  installer identity changed too, an install of an older version is not
  recognised as the same application and is not replaced by the upgrade; it can
  be uninstalled from Add/Remove Programs once the new one is running. Your
  catalogue is untouched by that — it lives in the data folder, which is
  migrated.
- **Library health now draws the pipeline as the chain it is.** The five status
  cards were a grid of equal tiles, which said the archive does five unrelated
  things at once; the one real relation between them — Indexing and Duplicates
  run first and everything else reads what they produce — was left to the words
  "Waiting for Duplicates…" inside a tile. They are now rows on a rail, with the
  two that always run marked as such and the rest hanging off them: the same
  chain the setup screen draws before any of it starts. Each row is full width,
  so it says more in less height than the tile it replaces.

- **The Library section is now called Browse.** Every other section is named
  for what it holds — People, Places, Duplicates — and this one is named for
  what you do there: look through the whole archive, by filter or by
  description. "Library" still means the collection itself, as in Library
  health on the Overview.

- **A feature is called the same thing everywhere, and carries the same mark.**
  The setup screen offered "Search by description"; Library health then reported
  on it as "Semantic indexing" and the sidebar announced it as "Indexing search…",
  and nothing on any of those screens said they were the same thing. Every card,
  chip and status line now takes its name from the feature you chose, and each
  feature has one icon — on the card you press, on the link in the pipeline, on
  the card reporting its progress, and on the section it unlocks. The shared
  People & pets card also stops naming the half you did not ask for: on a
  pets-only archive it says "Finding pets…" rather than promising people it was
  never going to look for.

- **Model weights are downloaded when you create the archive, not hours later.**
  The setup screen quotes what a feature costs to download, and the download then
  waited for the stage that needed it — which waits for scanning, dating and
  duplicate-finding to finish first. On a large folder that meant choosing Search
  by description in the morning and a 689 MB fetch quietly beginning in the
  afternoon, long after anyone was watching for it. It now starts as soon as the
  archive is created, runs alongside the first scan, and is usually finished
  before the stage that needs it is reachable. The sidebar shows what is
  downloading and how far along it is; features you did not choose are still
  never fetched, and an archive whose models are already here from another
  archive downloads nothing at all.

- **Search by description returns more of what you asked for, and less of what you
  didn't.** Two things were wrong with how weak matches were hidden. The scores
  themselves were squeezed into a narrow band, because the model places pictures
  and sentences in two separate regions of its vector space; measuring each from
  its own centre spreads them out roughly threefold. And of the two thresholds
  that decide what to show, one sat below every score it could ever see, leaving
  the other to do both jobs — the only way it could suppress a bad result was to
  tighten until good ones fell out too. On a test archive a search for "a lake"
  returned 7 of its 15 lake photos; it now returns them all, while a search for
  something the archive does not contain returns nearly nothing instead of a
  confident-looking page. Nothing is re-indexed.
- **The installers are roughly half the size.** Three things were shipping weight
  nobody could use: the People and Pets model weights travelled inside the download
  (349 MB) even though the app already knows how to fetch weights on demand; FFmpeg
  was bundled as two self-contained binaries that each carried a complete copy of
  the codec set; and the OpenCV build shipped a full Qt desktop toolkit for an app
  that never opens a window. The weights are now downloaded on first use like every
  other model, so the first People/Pets run fetches about 550 MB instead of 220 MB
  and needs a working connection once. Nothing else about the app changes.
- Face clustering no longer needs the `faiss-cpu` package. The search it did is
  now done directly in NumPy, producing identical people and identical groupings —
  a clustering pass is 1.3–2.3x slower, which is a few minutes against a detect run
  measured in hours, and the wheel was 62 MB of installer.
- Model downloads now report progress. A first run that fetches several hundred
  megabytes used to show one unchanging line on the stage card and read as hung.
- The Overview storage panel was reworked: one bar with a Size/Files switch
  instead of two competing bars, exact numbers moved into the table, and per-type
  share shown on hover. Either end of that switch flips it, rather than the lit
  end doing nothing.
- A stage card no longer shows a progress bar while the stage is still setting
  itself up. Counting a 150k-file folder or fetching model weights happens before
  a single file is processed, and a bar sitting at 0% across it read as a run that
  had hung; the card now says what it is preparing, and a model download's own
  percentage is the headline rather than a footnote beside the bar.
- The Overview's "All media" tile is called **All files**: it counts everything
  catalogued, documents and archives included, not just photos and video.
- Merging two people of the same size, neither of them named, now keeps the older
  of the two rather than depending on which card you dragged onto which — the rule
  pets and places already followed.

### Fixed

- The archive setup screen no longer carries a name between folders. Naming one
  archive and then adding a second offered the first one's name in the field,
  because the panel is hidden rather than rebuilt when it closes and the name was
  read back off it. A name typed while adding a feature is no longer lost either,
  in either direction: half-typed names now survive the panel re-rendering, and
  renaming an existing archive survives it too instead of reverting.
- The blinking cursor in the "Search by description" card on that screen is now
  still, like every other card's picture.

- Pausing responds immediately. A pause asks the running job to stop at its next
  batch checkpoint, which takes seconds, and the card went on reporting the work
  as if nothing had happened before stopping abruptly; it now says "Pausing…" the
  moment you press it. A stage stopped part-way also keeps the progress bar it
  reached, since the run is suspended rather than thrown away.
- Pausing no longer follows you to another archive. The pause was one app-wide
  switch, so stopping work on one folder left the next folder you opened stopped
  too, with nothing running and a Resume button nobody had pressed. Each archive
  now remembers its own pause, whole-pipeline and per-stage.

- Seeking in a video or audio file is reliable. The server mishandled two of the
  requests a player makes: asking for a position past the end of the file produced
  a malformed reply instead of a "that range does not exist" answer, and asking for
  the *last* few bytes — how a player finds the index in an MP4 that stores it at
  the end — returned bytes from the *start* of the file while labelling them as the
  end. Depending on the player, that showed up as a clip that would not scrub, or
  one that refused to open at all.

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
- `trove scan` crashed immediately with `UnboundLocalError` before doing any work,
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
