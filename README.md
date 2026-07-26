# Trove

Trove is a desktop catalogue for a large, messy media collection: family photos,
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
  map pin, and manually assigned without altering the media's GPS metadata. A spot
  needs at least 10 photos to count as a place (`place_min_media`), so a one-off
  snapshot somewhere random stays unplaced instead of cluttering the map; places you
  have named or pinned yourself always count, whatever their size.
- Detects and clusters faces locally into people. You can name people, correct a face,
  merge or separate suggested people, and dismiss non-person detections.
- Detects cats, dogs, birds, and horses locally and groups conservative likely-pet
  identities. People and animals are found in the same pass and cross-check each
  other: an animal's own face is kept out of People, while a person who is not
  upright in the frame — lying down, or a photo stored sideways — is kept out of
  Pets rather than being catalogued as a dog.
- Looks inside videos too. Several keyframes are sampled per clip and both detectors
  run on each; the detections then collapse per video, so one person appearing across
  five frames is one result rather than five.
- Merges and unmerges people or pets. Drag one card onto another to merge them; the
  hovered card says what the drop will do and the merge is confirmed before it
  happens. A merge can be undone afterwards, and a single wrongly attached photo can
  be detached from a person on its own. One limit worth knowing: undo restores a merge
  *you* made, but cannot split a group the automatic pass formed by itself.
- Lets you attach a person or pet to a file by hand, for media where nothing was
  detected — a back-of-the-head shot, a photo too dark to detect, a scanned print.
  Manual tags reference people and pets by name, so they survive the automatic
  re-clustering that runs as the catalogue grows.
- Searches the library by description when optional semantic indexing is enabled
  (see below).
- Shows photos the right way up. EXIF orientation is always honoured, and a photo
  whose pixels are stored turned while its EXIF says otherwise (common among
  re-exports of the same shot) is recognised by the detectors and displayed
  upright. The file on disk is never modified. This only applies where there is
  solid evidence — several faces that line up once the photo is turned, or a
  person filling the frame — so photos of scenery, documents or distant subjects
  are shown exactly as stored.
- Generates cached thumbnails and serves the interface only on `127.0.0.1`.

When an archive is open in the app, its pipeline runs automatically. Scanning and
metadata extraction overlap, and once duplicates are grouped the three remaining
stages no longer depend on each other:

```text
scan ┐
     ├─→ duplicate grouping ─┬─→ people & pets (one detection pass)
metadata ┘                   ├─→ places
                             └─→ semantic indexing (optional)
```

Long stages commit progress in batches. Closing or switching archives asks current
work to stop at a safe checkpoint; reopening resumes it rather than restarting. The
whole pipeline can be paused and resumed from the library health panel — running
jobs stop at their next checkpoint, and the pause survives a restart, which is
useful when the machine is needed for something else. The sidebar lists every stage
currently running, not just one.

## Privacy and optional embeddings

The normal catalogue is local-first. Scanning, hashing, metadata extraction,
duplicates, thumbnails, place clustering, face detection, face embeddings, face
clustering, and the SQLite catalogue all stay on the machine. Trove has no telemetry
and does not modify source media. Enabling the map's street-map layer fetches public
map tiles online, which discloses the viewed coordinates but never uploads photos.

Optional multimodal embedding is the one exception. If `VOYAGE_API_KEY` is available,
Trove automatically indexes compatible canonical media with Voyage Multimodal and
stores the returned vectors locally in SQLite. It sends a downscaled cached JPEG for
images where possible, or the original compatible MP4 video; it never sends an
original image merely because a thumbnail was available. Audio, PDFs, other document
formats, and non-MP4 video are recorded as skipped. Text queries sent to the semantic
endpoint also leave the machine. Do not set this key unless that data transfer is
appropriate for the archive.

In the desktop app, paste the key into **Settings**. It is written to a
`secrets.json` in the application-data folder with owner-only permissions and loaded
into the app process at startup. From a source checkout you can instead put it in a
project-root `.env` file, or set it in the environment before launching:

```text
VOYAGE_API_KEY=...
```

The key is deliberately kept out of Trove's `config.json`, which is otherwise
readable configuration. Embedding is resumable, does not process hidden duplicates,
and can run alongside the local pipeline. Remove the key and indexing simply stops;
vectors already stored stay searchable.

## Install and run the desktop app

Desktop builds produce Linux x64 AppImage and Debian/Ubuntu packages, plus an NSIS
Windows installer. Build and test packages on their target operating system; see the
release guide for the current publication and signing requirements.

### Linux

For an AppImage:

```bash
chmod +x Trove-<version>.AppImage
./Trove-<version>.AppImage
```

For Debian or Ubuntu:

```bash
sudo apt install ./trove-desktop_<version>_amd64.deb
```

Linux packages bundle FFmpeg and FFprobe. ExifTool is optional: without it, Trove
still uses Takeout sidecars, filenames, and file timestamps, but cannot read the full
range of embedded metadata.

See [Linux installation notes](docs/install-linux.md) for AppImage/FUSE and data-path
details. See [Windows installation notes](docs/install-windows.md) for installer,
signature, and uninstall behavior.

### First use

Open Trove and choose the folder containing your media. Opening that archive starts
the automatic pipeline. You can add additional folders from the archive picker. An
archive whose drive is disconnected remains registered and is shown as unavailable;
mount it again to continue.

Removing an archive from Trove removes its catalogue records and derived cache for
that archive after background work has stopped. It does not remove the selected source
folder or any file under it.

## Command line and source setup

Trove requires Python 3.13 or newer. Create a virtual environment and install the
package with the extras appropriate to the features you want:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[cli,media,faces,pets]'
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
oa pets
oa faces
oa gui
```

Useful companion commands are `oa status`, `oa dates`, `oa config --show`, and
`oa migrate-data` for copying an older project-local `data/` directory into the
per-user data location. All long commands are designed to be re-run.

Face extraction rejects low-confidence, tiny, blurry, severely over/underexposed,
and substantially clipped candidates before they enter People. Inspect persisted
decision counts and post-clustering unassigned noise with
`oa faces --quality-report`, or test the configured thresholds against up to 100
pending images without changing the catalog:

```bash
oa faces --calibrate 100
```

`oa gui` starts the local interface at `http://127.0.0.1:8756/`; it opens a standalone
browser window when a supported Chromium-family browser is available. Use `--tab` to
open a normal tab or `--no-open` when launching it remotely.

The native "choose folder" dialog is provided by the desktop app; in a plain browser
(`oa gui`) there is no OS picker, so **type the absolute folder path** into the box on
the welcome screen (e.g. `/mnt/photos/Multimedia` or `D:\Photos`) and press *Choose media
folder*. Use the packaged desktop app if you want to click through a folder dialog.

## Data locations and backups

Trove keeps mutable data outside both the source archive and the installed app:

| Platform | Default location |
| --- | --- |
| Linux | `$XDG_DATA_HOME/organize_archive`, normally `~/.local/share/organize_archive` |
| Windows | `%LOCALAPPDATA%\organize_archive` |
| macOS | `~/Library/Application Support/organize_archive` |

Inside it, each archive you add is fully isolated in `archives/<id>/`, with its own
`archive.db` and its own thumbnail and face-crop cache, so one archive can be removed
without touching another. Shared across all of them are `config.json`, `secrets.json`
(the optional API key, owner-readable only), and the downloaded machine-learning
models, which are large and worth keeping.

The whole directory is valuable derived data: back it up by copying it while Trove is
closed. Restoring it does not change the original media — at worst you re-scan.

The directory is still named `organize_archive` rather than `Trove`. That is
deliberate: the product name changed, but the package, CLI, application id and data
path did not, so catalogues built by earlier versions keep working.

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

Packaging first needs its two staged inputs, which are downloaded and SHA-256
verified against manifests in `packaging/` rather than committed:

```bash
python3 packaging/scripts/stage-tools.py --target linux-x64   # or win32-x64
python3 packaging/scripts/stage-models.py
```

`npm run build:backend` refuses to run until both have been staged, so a build
cannot silently ship without ffmpeg/ffprobe or without the bundled pet re-ID
model. See [the release guide](docs/release.md#build-inputs) for what each input
is and where it comes from.

**`npm run dev` must reach the project virtualenv.** In development the shell launches
the backend with plain `python3`. If that interpreter is the system Python rather than
the project `.venv`, OpenCV and onnxruntime are missing and **Pets and People report
"unavailable"** (the rest of the app still works). Activate the venv first, or point the
shell at it explicitly:

```bash
# either activate the venv before launching
. ../.venv/bin/activate && npm run dev
# …or name the interpreter for this run
PYTHON=../.venv/bin/python npm run dev   # Windows: set PYTHON=..\.venv\Scripts\python.exe
```

Packaged builds are unaffected — they bundle their own interpreter and models.

Native-tool staging and packaging files are in `packaging/`. Release versioning and
clean-machine checks are described in [the release guide](docs/release.md).

## Current limitations

- Visual duplicate matching applies to images; video near-duplicate matching is not
  implemented.
- People and pets are found in videos by sampling a few keyframes, so a face that
  appears only briefly between those frames can be missed.
- Detection models are downloaded once into the cache the first time they are needed,
  so a new installation needs network access once. Everything after that is local.
- Automatic people grouping is reliable on collections up to a few tens of thousands
  of photos. Beyond that, noisy detections increasingly bridge distinct clusters, and
  separate people can be merged into one group. Reviewing and naming people
  constrains this, and the manual merge/unmerge tools let you correct it.
- Embedded metadata quality depends on installed tools and the source formats.
- Optional Voyage indexing accepts images and MP4 video only; it intentionally skips
  audio, PDFs, documents, and unsupported video formats.
- The bundled Linux release tools do not include ExifTool.

For recovery and common failures, see [troubleshooting](docs/troubleshooting.md).

## License

MIT — see [LICENSE](LICENSE).

Trove bundles or downloads third-party components (ffmpeg, ExifTool, Leaflet,
Bergamot, and several machine-learning models) under their own licenses. These
are listed in
[packaging/THIRD_PARTY_NOTICES.md](packaging/THIRD_PARTY_NOTICES.md).
