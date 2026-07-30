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
  be shown again. The Duplicates page breaks the redundant copies down by what they
  are — byte-identical versus only visually the same, and photos versus the far
  fewer videos that tend to account for most of the reclaimable space.
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
whole pipeline can be paused and resumed from the library health panel, and each
stage has its own pause button for stopping just that one (say, people & pets)
while the rest keeps going — running jobs stop at their next checkpoint, and both
kinds of pause survive a restart, which is useful when the machine is needed for
something else. The sidebar lists every stage currently running, not just one.

## Privacy

Everything is local. Scanning, hashing, metadata extraction, duplicates,
thumbnails, place clustering, face detection and clustering, search by
description, and the SQLite catalogue all stay on the machine. Trove has no
telemetry, no accounts, no API keys, and does not modify source media.

**The map's street-map layer is the only outbound network call in the app.**
Turning it on fetches public map tiles, which discloses the coordinates you are
looking at — never the photos themselves. It is a toggle, and switching it off
leaves a fully offline plot.

Search by description works by embedding your media and your query with
[SigLIP 2](https://huggingface.co/google/siglip2-base-patch16-256) (Apache-2.0),
which runs on this machine like every other model here. The model downloads
itself once, about 690 MB, the first time an archive is indexed; after that
nothing is fetched. Photos are embedded through the same cached thumbnails the
app already displays, videos through a few sampled frames. Audio and documents
are recorded as skipped — the model has no audio tower. Indexing is resumable,
skips hidden duplicates, and runs alongside the rest of the pipeline.

## Install

| Platform | Download | Size |
| --- | --- | --- |
| Windows 10/11 (64-bit) | [Trove.Setup.0.1.2.exe](https://github.com/gapsa-0/Trove/releases/download/v0.1.2/Trove.Setup.0.1.2.exe) | 604 MB |
| Linux, any distribution | [Trove-0.1.2.AppImage](https://github.com/gapsa-0/Trove/releases/download/v0.1.2/Trove-0.1.2.AppImage) | 744 MB |
| Debian / Ubuntu | [trove-desktop_0.1.2_amd64.deb](https://github.com/gapsa-0/Trove/releases/download/v0.1.2/trove-desktop_0.1.2_amd64.deb) | 614 MB |

The downloads are large because each one bundles its own Python runtime, FFmpeg,
and the local detection models — nothing is fetched from a package manager at
install time.

Checksums for all three are in
[SHA256SUMS.txt](https://github.com/gapsa-0/Trove/releases/download/v0.1.2/SHA256SUMS.txt).
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
and your catalogue is kept in `%LOCALAPPDATA%\organize_archive` in case you reinstall
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

See [Linux installation notes](docs/install-linux.md) for AppImage/FUSE and data-path
details.

### First use

Open Trove and choose the folder containing your media. Opening that archive starts
the automatic pipeline. You can add additional folders from the archive picker. An
archive whose drive is disconnected remains registered and is shown as unavailable;
mount it again to continue.

The first time People and Pets detection runs, Trove downloads their model weights
(about 220 MB) once. After that it works offline; all media processing is local from
the start.

Removing an archive from Trove removes its catalogue records and derived cache for
that archive after background work has stopped. It does not remove the selected source
folder or any file under it.

If something goes wrong, see [troubleshooting](docs/troubleshooting.md).

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

## Building from source

```bash
git clone https://github.com/gapsa-0/Trove.git && cd Trove
make setup          # venv + every extra, at the tested versions
make check          # lint and the full test suite — what CI runs
```

`make` on its own lists the other targets. If your system has no `python3.13`,
pass one: `make setup PYTHON=/path/to/python3.13`.

Using the `oa` command line and building the desktop packages yourself are covered
in [command line and development](docs/command-line.md); which optional extra
enables what, and why each dependency was chosen, is in
[dependencies](docs/dev/dependencies.md).

## License

MIT — see [LICENSE](LICENSE).

Trove bundles or downloads third-party components (ffmpeg, ExifTool, Leaflet,
Bergamot, and several machine-learning models) under their own licenses. These
are listed in
[packaging/THIRD_PARTY_NOTICES.md](packaging/THIRD_PARTY_NOTICES.md).
