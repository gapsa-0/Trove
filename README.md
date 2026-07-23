# organize_archive

A local, read-only cataloging tool for a large and messy family multimedia archive —
photos, videos, audio, documents — scattered across nested folders, phone dumps, and
multiple Google Takeout exports.

It builds a **database about your files** so you can find and navigate them, **without
ever moving or changing a single original**. A visual GUI is planned; today it's a
Python library and command-line tool.

## Why

Decades of family media end up as an unnavigable pile: the same photo copied into five
folders, dates hidden inconsistently (sometimes in EXIF, sometimes in the filename,
sometimes only in a Google sidecar), a dozen file formats, and no way to ask simple
questions like *"show me everything from the 2022 Bariloche trip"* or *"every photo of
Grandma"*.

This project catalogs the mess in place and makes it queryable.

## Principles

- **Originals are never touched.** No moves, no renames, no edits, no files written next
  to your media. Everything derived lives in a single SQLite database and a cache folder.
- **Everything runs on your machine.** No cloud, no uploads — now or when face
  recognition and AI features arrive. Your family's photos stay private.
- **Built for a real, large archive.** ~500 GB and ~150k files here: scanning is
  resumable, incremental, and safe to interrupt.
- **Honest metadata.** Every fact the tool infers (a date, a location, a file type)
  records where it came from and how confident it is.

## Features

### Available (core roadmap)
- **Scan & index** — walk the archive, hash files, read metadata, store it all in SQLite.
  Resumable and incremental.
- **Deduplication** — find identical files (exact) *and* the same photo re-compressed or
  saved in a different format across different takeouts (perceptual). Copies are grouped,
  a best "canonical" version is chosen, and the rest are hidden from browsing —
  **nothing is ever deleted.**
- **Date navigation** — one reliable date per file, resolved from the best available
  source (Google Takeout JSON → EXIF → filename → file timestamp), then browse by
  year / month / range.
- **Type & folder navigation** — filter by media type (image / video / audio / document)
  and by original source folder.

### Planned (later)
- **Face recognition** (local) — pick a person (or several) and retrieve their photos,
  later videos.
- **Map view** — plot photos/videos by their GPS coordinates.
- **AI descriptions & semantic search** — local embeddings to search media by content
  ("beach sunset", "birthday cake").
- **Timeline** view and **pet detection**.
- **GUI** for visual browsing.

## Requirements

- Python 3.13
- System tools: `exiftool` and `ffmpeg`/`ffprobe` (used for reading metadata and video
  frames). The tool will tell you clearly if they're missing.
- Python dependencies are declared in `pyproject.toml` (install into a virtualenv).

## Desktop development

Electron owns the window and native folder picker; the bundled Python backend remains
loopback-only. From `desktop/`:

```text
npm install
npm run dev
npm run build:backend
npm run package:linux   # Linux: AppImage + .deb
npm run package:win     # Windows: NSIS installer
```

Build on the target OS. A public Windows release also needs code signing; unsigned
installers may trigger SmartScreen. The packaging profile and native-tool handoff are
in `packaging/`.

## Status

Early development. The database core, indexer, and deduplication are the current focus.
See **[TODO.md](TODO.md)** for the roadmap and **[CLAUDE.md](CLAUDE.md)** for the
technical design and contributor guidance. Public-beta installation and recovery
guides: [Windows](docs/install-windows.md), [Linux](docs/install-linux.md),
[privacy/data](docs/privacy-and-data.md), and [troubleshooting](docs/troubleshooting.md).

## Configuration

New installations start without archive roots. Add one with
`oa config --add-root PATH` (multiple roots are supported), then run `oa init`.
On Linux, configuration, the database, and caches live in
`$XDG_DATA_HOME/organize_archive` (or `~/.local/share/organize_archive`); Windows
uses `%LOCALAPPDATA%\\organize_archive`. Nothing is written next to your originals or
the installed application. To copy data from a prior project-local `data/` directory,
run `oa migrate-data` (or provide its location with `--from PATH`); the original is
kept unchanged.
