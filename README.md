# Trove

[![CI](https://github.com/gapsa-0/Trove/actions/workflows/ci.yml/badge.svg)](https://github.com/gapsa-0/Trove/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/gapsa-0/Trove)](https://github.com/gapsa-0/Trove/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Trove is a desktop catalogue for a large, messy media collection: family photos,
videos, audio, documents, phone dumps, and Google Takeout exports spread across one
or more folders. It indexes the collection in place so it can be browsed by date,
person, place, type, folder, and duplicate group.

It never moves, renames, edits, or deletes an original file. The catalogue, thumbnails,
face crops, models, and other derived data live separately in the current user's
application-data folder.

![The Library overview: counts for the whole archive, a card per pipeline stage
with its own pause control, and storage broken down by media
type.](docs/images/overview.png)

*The archive above is the small synthetic one the test suite uses, which is why
the numbers are tiny — it is the interface that is being shown, not a real
library.*

## What it does

- Adds one or more archive folders and keeps a separate catalogue for each one.
- Asks each archive what it should do with the folder. Indexing and duplicates
  always run — everything else reads what they produce — and the rest are
  chosen on a setup screen: People, Pets, Places, Search by description, Search
  by document text, and Search by picture text. A feature you leave off never runs and never downloads its models, which is the
  difference between an archive that starts working immediately and one that
  fetches 715 MB first. The choice can be changed at any time, and switching a
  feature off keeps whatever it already found.
- Scans incrementally and resumably. Unchanged files are skipped on later runs;
  interrupted work can continue safely.
- Reads Google Takeout sidecars, embedded metadata, filenames, and file timestamps.
  Each resolved date and GPS value retains its source.
- Chooses a best capture date using this default order: Google Takeout capture time,
  EXIF, filename, then filesystem modification time.
- Groups byte-identical copies and visually similar image exports. A canonical copy
  is selected; the others are hidden from normal browsing but never deleted, and each
  one stays listed, openable and named on the Duplicates page. That page breaks the
  redundant copies down by what they are — byte-identical versus only visually the
  same, and photos versus the far fewer videos that tend to account for most of the
  reclaimable space — and it can read the split by size or by file count. Freeing the
  space is your job: Trove has no delete button anywhere, so it tells you which copies
  are spare and which folder each one is in, and leaves them where they are.
- Builds a timeline, media library, source-folder view, and item inspector.
- Clusters GPS-tagged media into places. Places can be named, manually created from a
  map pin, and manually assigned without altering the media's GPS metadata. A spot
  needs at least 10 photos to count as a place (`place_min_media`), so a one-off
  snapshot somewhere random stays unplaced instead of cluttering the map; places you
  have named or pinned yourself always count, whatever their size. The map switches
  between that grouped view and one dot per photo, coloured by place, for when the
  question is where each shot was actually taken rather than where you keep going back.
- Detects and clusters faces locally into people. You can name people, correct a face,
  merge or separate suggested people, and dismiss non-person detections. Faces are
  quality-checked before they are ever grouped: blurry, tiny, and badly-framed
  detections are set aside instead of being allowed to blur two people together,
  so a person's photos stay one person and stray detections stay out of the way.
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
- Searches the library by description when that optional feature is enabled
  (see below).
- Reads the text inside documents, when the optional Search by document text
  feature is enabled, so a phrase finds the file: PDFs that carry a text layer, Word, Excel and
  PowerPoint files, OpenDocument files, plain text, Markdown, CSV, web pages and
  notebooks. Results show the passage that matched and the page it was on, and a
  search for `peticion` finds `petición`. Nothing is downloaded for it — every
  reader is the Python standard library or the PDF library the app already
  bundles. Two limits are worth knowing: a scanned PDF is pictures of a page with
  no text in the file at all, so this finds nothing in it (that is what the
  feature below is for), and pre-2007 `.doc`, `.xls` and `.ppt`
  files cannot be read.
- Reads the writing in pictures, when the optional Search by picture text feature
  is enabled: photographed receipts, screenshots, and PDFs that are scans rather
  than documents. A PDF is decided page by page, so a contract with a scanned
  appendix is read both ways and comes back as one document. Spanish and English,
  accents included. It downloads 30 MB the first time you switch it on, once per
  computer rather than once per archive. It is the slowest stage in the pipeline, at
  roughly half a second per picture, so an archive of a hundred thousand photos
  is an overnight run rather than a coffee break; it pauses and resumes safely.
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
     ├─→ duplicate grouping ─┬─→ places
metadata ┘                   ├─→ people & pets (one detection pass)
                             ├─→ description index (optional)
                             └─→ reading document + picture text (optional)
```

Long stages commit progress in batches. Closing or switching archives asks current
work to stop at a safe checkpoint; reopening resumes it rather than restarting. The
whole pipeline can be paused and resumed from the library health panel, and each
stage has its own pause button for stopping just that one (say, people & pets)
while the rest keeps going — running jobs stop at their next checkpoint, and both
kinds of pause survive a restart, which is useful when the machine is needed for
something else. The sidebar lists every stage currently running, not just one.

## Privacy

Everything is local. Scanning, hashing, metadata extraction, duplicates,
thumbnails, place clustering, face detection and clustering, search by
description, reading the text inside documents and pictures, and the SQLite
catalogue all stay on the machine. Trove has no telemetry, no accounts, no API
keys, and does not modify source media.

**The map's street-map layer is the only outbound network call in the app.**
Turning it on fetches public map tiles, which discloses the coordinates you are
looking at — never the photos themselves. It is a toggle, and switching it off
leaves a fully offline plot.

Search by description works by embedding your media and your query with
[SigLIP 2](https://huggingface.co/google/siglip2-base-patch16-256) (Apache-2.0),
which runs on this machine like every other model here. The model downloads
itself once, about 690 MB, as soon as you create an archive that asks for it —
alongside that archive's first scan, rather than at the end of it; after that
nothing is fetched. Photos are embedded through the same cached thumbnails the
app already displays, videos through a few sampled frames. Audio and documents
are recorded as skipped — the model has no audio tower. Indexing is resumable,
skips hidden duplicates, and runs alongside the rest of the pipeline.

A Spanish search is translated to English on this machine before it is
embedded, which is not the redundancy it looks like. The model reads text
*inside* your pictures, so a Spanish query drifts toward Spanish-language
screenshots and memes rather than photographs; and although it is multilingual,
it was trained overwhelmingly on English, so a Spanish query scores low enough
to be mistaken for having no match at all. The translation model runs locally
too — nothing about a search leaves the machine.

## Install

| Platform | Download | Size |
| --- | --- | --- |
| Windows 10/11 (64-bit) | [Trove.Setup.0.3.0.exe](https://github.com/gapsa-0/Trove/releases/download/v0.3.0/Trove.Setup.0.3.0.exe) | 312 MB |
| Linux, any distribution | [Trove-0.3.0.AppImage](https://github.com/gapsa-0/Trove/releases/download/v0.3.0/Trove-0.3.0.AppImage) | 395 MB |
| Debian / Ubuntu | [trove-desktop_0.3.0_amd64.deb](https://github.com/gapsa-0/Trove/releases/download/v0.3.0/trove-desktop_0.3.0_amd64.deb) | 269 MB |

Each download bundles its own Python runtime and FFmpeg — nothing is fetched from
a package manager at install time. **No model weights are in there.** Every one is
downloaded once, the first time you switch on the feature that needs it, and
shared by every archive on the machine — so a feature you never enable costs you
nothing to have installed.

Checksums for all three are in
[SHA256SUMS.txt](https://github.com/gapsa-0/Trove/releases/download/v0.3.0/SHA256SUMS.txt).
Newer versions, when they exist, are on the
[releases page](https://github.com/gapsa-0/Trove/releases).

### Windows

**Windows will warn you the first time.** Trove is not code-signed, so SmartScreen
shows a blue *"Windows protected your PC"* panel with only a *Don't run* button in
sight. This is what Windows does for any application from an independent developer
without a paid signing certificate; it is not a report that anything is wrong with
the file. Click **More info**, then **Run anyway**. If you want to satisfy yourself
first, check the download against `SHA256SUMS.txt` on the release page — in
PowerShell, or with `certutil -hashfile "Trove.Setup.0.1.2.exe" SHA256` in Command
Prompt:

```powershell
Get-FileHash 'Trove.Setup.0.1.2.exe' -Algorithm SHA256
```

Then run the installer and follow the prompts. It installs for your user only, so it
needs no administrator rights, and it creates a desktop and Start-menu shortcut.

Uninstalling removes the application and its shortcuts. It never touches your media,
and your catalogue is kept in `%LOCALAPPDATA%\trove` in case you reinstall
— delete that folder yourself if you want it gone.

See [Windows installation notes](docs/install-windows.md) for signature verification
and further detail.

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

Linux packages bundle FFmpeg and FFprobe. ExifTool is not included on Linux: without
it, Trove still uses Takeout sidecars, filenames, and file timestamps, but cannot read
the full range of embedded metadata.

On Ubuntu 23.10 and later, prefer the `.deb` if you have the choice. It installs an
AppArmor profile that lets Chromium sandbox the part of Trove that renders your
media; an AppImage installs nothing, so it cannot, and runs that part unsandboxed
on those systems. Both work — the difference is one layer of defence, and it is
explained in the Linux notes below.

See [Linux installation notes](docs/install-linux.md) for AppImage/FUSE and data-path
details.

### First use

Open Trove and choose the folder containing your media. Opening that archive starts
the automatic pipeline. You can add additional folders from the archive picker. An
archive whose drive is disconnected remains registered and is shown as unavailable;
mount it again to continue.

Creating an archive with People or Pets switched on downloads their model weights
(about 550 MB) once, while that archive's first scan runs. After that it works
offline; all media processing is local from the start.

Removing an archive from Trove removes its catalogue records and derived cache for
that archive after background work has stopped. It does not remove the selected source
folder or any file under it.

If something goes wrong, see [troubleshooting](docs/troubleshooting.md).

## Data locations and backups

Trove keeps mutable data outside both the source archive and the installed app:

| Platform | Default location |
| --- | --- |
| Linux | `$XDG_DATA_HOME/trove`, normally `~/.local/share/trove` |
| Windows | `%LOCALAPPDATA%\trove` |
| macOS | `~/Library/Application Support/trove` |

Inside it, each archive you add is fully isolated in `archives/<id>/`, with its own
`archive.db` and its own thumbnail and face-crop cache, so one archive can be removed
without touching another. Shared across all of them are `config.json` and the
downloaded machine-learning models, which are large and worth keeping.

Older installs may also have a `secrets.json` holding the API key an earlier
build needed for cloud embedding. There is nothing to put in it now — every
model runs locally — so Trove deletes it at startup rather than leaving a live
credential on disk for a feature that no longer exists.

The whole directory is valuable derived data: back it up by copying it while Trove is
closed. Restoring it does not change the original media — at worst you re-scan.

Before the rename this directory was called `organize_archive`. If you are upgrading
from such a build, Trove moves it to the new name the first time it starts and
repoints the paths recorded inside `config.json`, so your catalogue carries over
without a re-scan. The move happens once; nothing is copied, so it is instant
however large the catalogue is. If both directories somehow exist, Trove leaves
both alone, starts from the new one, and says so in the log.

## Building from source

```bash
git clone https://github.com/gapsa-0/Trove.git && cd Trove
make setup          # venv + every extra, at the tested versions
make check          # lint, the test suite, and the browser tier — what CI runs
```

`make` on its own lists the other targets. If your system has no `python3.13`,
pass one: `make setup PYTHON=/path/to/python3.13`. Building the desktop app needs
the Node version in `.nvmrc`; `npm ci` refuses anything else.

Then run it — `cd desktop && PYTHON=../.venv/bin/python npm run dev` for the desktop
app, or `make gui` for the same interface in a browser against a throwaway data
directory. [command line and development](docs/command-line.md) has the detail.

For anything beyond that:

- [ARCHITECTURE.md](ARCHITECTURE.md) — what the pieces are, how data flows through
  them, and where to go to change a given thing.
- [CONTRIBUTING.md](CONTRIBUTING.md) — commit rules, the definition of done, where
  a new test belongs, and how to look at a GUI change.
- [docs/adr/](docs/adr/) — why the larger decisions were made, one record each.
- [CHANGELOG.md](CHANGELOG.md) — what changed between releases.
- [command line and development](docs/command-line.md) — using `trove`, and building
  the desktop packages yourself.
- [dependencies](docs/dev/dependencies.md) — which optional extra enables what, and
  why each dependency was chosen.
- [SECURITY.md](SECURITY.md) — the localhost server's threat model, and how to
  report a vulnerability.

Trove is an early, single-developer project with no support commitment. There is
no public roadmap; the changelog is the record of what has actually landed.

## License

MIT — see [LICENSE](LICENSE).

Trove bundles or downloads third-party components (ffmpeg, ExifTool, Leaflet,
Bergamot, and several machine-learning models) under their own licenses. These
are listed in
[packaging/THIRD_PARTY_NOTICES.md](packaging/THIRD_PARTY_NOTICES.md).
