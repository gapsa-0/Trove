# Archive

Archive is a desktop catalogue for a large, messy media collection: family photos,
videos, audio, documents, phone dumps, and Google Takeout exports spread across one
or more folders. It indexes the collection in place so it can be browsed by date,
person, place, type, folder, and duplicate group.

It never moves, renames, edits, or deletes an original file. The catalogue, thumbnails,
face crops, models, and other derived data live separately in the current user's
application-data folder.

## What it does

- Adds one or more archive folders and keeps a separate catalogue for each one.
- Scans incrementally and resumably. Unchanged files are skipped on later runs;
  interrupted work can continue safely.
- Reads Google Takeout sidecars, embedded metadata, filenames, and file timestamps.
  Each resolved date and GPS value retains its source.
- Chooses a best capture date using this default order: Google Takeout capture time,
  EXIF, filename, then filesystem modification time.
- Groups byte-identical copies and visually similar image exports. A canonical copy
  is selected; other copies are hidden from normal browsing, never deleted, and can
  be shown again.
- Builds a timeline, media library, source-folder view, and item inspector.
- Clusters GPS-tagged media into places. Places can be named, manually created from a
  map pin, and manually assigned without altering the media's GPS metadata.
- Detects and clusters faces locally into people. You can name people, correct a face,
  merge or separate suggested people, and dismiss non-person detections.
- Generates cached thumbnails and serves the interface only on `127.0.0.1`.

When an archive is open in the app, its pipeline runs automatically:

```text
scan → metadata and date extraction → duplicate grouping → face processing → places
```

Long stages commit progress in batches. Closing or switching archives asks current
work to stop at a safe checkpoint; reopening resumes it. The interface shows the
active stage and progress.

## Privacy and optional embeddings

The normal catalogue is local-first. Scanning, hashing, metadata extraction,
duplicates, thumbnails, place clustering, face detection, face embeddings, face
clustering, and the SQLite catalogue all stay on the machine. Archive has no telemetry
and does not modify source media. Enabling the map's street-map layer fetches public
map tiles online, which discloses the viewed coordinates but never uploads photos.

Optional multimodal embedding is the one exception. If `VOYAGE_API_KEY` is available,
Archive automatically indexes compatible canonical media with Voyage Multimodal and
stores the returned vectors locally in SQLite. It sends a downscaled cached JPEG for
images where possible, or the original compatible MP4 video; it never sends an
original image merely because a thumbnail was available. Audio, PDFs, other document
formats, and non-MP4 video are recorded as skipped. Text queries sent to the semantic
endpoint also leave the machine. Do not set this key unless that data transfer is
appropriate for the archive.

Put the key in a project-root `.env` file or in the environment of the app process:

```text
VOYAGE_API_KEY=...
```

The key is deliberately not stored in Archive's `config.json`. Embedding is resumable,
does not process hidden duplicates, and can run alongside the local pipeline.

## Install and run the desktop app

Desktop builds produce Linux x64 AppImage and Debian/Ubuntu packages, plus an NSIS
Windows installer. Build and test packages on their target operating system; see the
release guide for the current publication and signing requirements.

### Linux

For an AppImage:

```bash
chmod +x Archive-<version>.AppImage
./Archive-<version>.AppImage
```

For Debian or Ubuntu:

```bash
sudo apt install ./organize-archive-desktop_<version>_amd64.deb
```

Linux packages bundle FFmpeg and FFprobe. ExifTool is optional: without it, Archive
still uses Takeout sidecars, filenames, and file timestamps, but cannot read the full
range of embedded metadata.

See [Linux installation notes](docs/install-linux.md) for AppImage/FUSE and data-path
details. See [Windows installation notes](docs/install-windows.md) for installer,
signature, and uninstall behavior.

### First use

Open Archive and choose the folder containing your media. Opening that archive starts
the automatic pipeline. You can add additional folders from the archive picker. An
archive whose drive is disconnected remains registered and is shown as unavailable;
mount it again to continue.

Removing an archive from Archive removes its catalogue records and derived cache for
that archive after background work has stopped. It does not remove the selected source
folder or any file under it.

## Command line and source setup

Archive requires Python 3.13 or newer. Create a virtual environment and install the
package with the extras appropriate to the features you want:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[cli,media,faces]'
```

`exiftool` and `ffmpeg`/`ffprobe` are recommended system tools. The core scanner works
without them, with reduced embedded-metadata and video support.

The CLI exposes the same durable catalogue operations:

```bash
oa config --add-root /path/to/archive
oa config --set-timezone America/Argentina/Buenos_Aires
oa init
oa scan
oa enrich
oa dedup
oa faces
oa gui
```

Useful companion commands are `oa status`, `oa dates`, `oa config --show`, and
`oa migrate-data` for copying an older project-local `data/` directory into the
per-user data location. All long commands are designed to be re-run.

`oa gui` starts the local interface at `http://127.0.0.1:8756/`; it opens a standalone
browser window when a supported Chromium-family browser is available. Use `--tab` to
open a normal tab or `--no-open` when launching it remotely.

## Data locations and backups

Archive keeps mutable data outside both the source archive and the installed app:

| Platform | Default location |
| --- | --- |
| Linux | `$XDG_DATA_HOME/organize_archive`, normally `~/.local/share/organize_archive` |
| Windows | `%LOCALAPPDATA%\organize_archive` |
| macOS | `~/Library/Application Support/organize_archive` |

This directory contains `archive.db`, `config.json`, cached thumbnails, face/model
assets, and logs. It is valuable derived data: back it up by copying the directory
while Archive is closed. Restoring it does not change the original media.

## Build the desktop app

The Electron shell owns the native window and folder picker; the Python backend stays
loopback-only. From `desktop/`:

```bash
npm install
npm run dev
npm run build:backend
npm run package:linux
npm run package:win
```

Native-tool staging and packaging files are in `packaging/`. Release versioning and
clean-machine checks are described in [the release guide](docs/release.md).

## Current limitations

- Visual duplicate matching applies to images; video near-duplicate matching is not
  implemented.
- Face detection applies to images, not video frames. Its local model is downloaded
  once into the cache when needed.
- Embedded metadata quality depends on installed tools and the source formats.
- Optional Voyage indexing accepts images and MP4 video only; it intentionally skips
  audio, PDFs, documents, and unsupported video formats.
- The bundled Linux release tools do not include ExifTool.

For recovery and common failures, see [troubleshooting](docs/troubleshooting.md).
