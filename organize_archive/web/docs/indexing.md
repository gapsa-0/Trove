---
title: Indexing
summary: Which files are catalogued, and how a date is worked out for each one.
feature: index
---

Indexing is the stage that makes the archive exist. It walks the folder you
added, records one row for every file it finds, and resolves a date for each
one. Everything else on these pages is built on what it produces.

It reads, and only reads. Nothing is moved, renamed, edited or converted, and
nothing is written into your folder.

## What gets catalogued

**Every file, in every subfolder, to any depth.** The walk starts at the folder
you chose and visits everything under it. Folder structure is never
interpreted: names like `2019`, `Camera`, or `New Folder (3)` mean nothing to
Trove, and no arrangement of files is better or worse than another.

Symbolic links are not followed, for directories or files. A folder linked into
your archive from somewhere else is not walked, which keeps one copy of a photo
from being catalogued twice and makes a link loop impossible. A directory Trove
cannot read, because of permissions or because it vanished mid-scan, is skipped
and the walk carries on.

Each file is sorted into one of six kinds by its extension. The kind is what
the Browse filters and the Storage panel group by:

| Kind | Extensions |
| --- | --- |
| Image | jpg, jpeg, png, gif, webp, bmp, tif, tiff, heic, heif, jfif, raw, cr2, nef, arw, dng |
| Video | mp4, mov, avi, wmv, mkv, 3gp, 3g2, m4v, mpg, mpeg, flv, webm, mts, m2ts, swf |
| Audio | opus, mp3, m4a, aac, wav, flac, ogg, amr, wma |
| Document | pdf, doc, docx, xls, xlsx, ppt, pptx, odt, ods, odp, txt, csv, html, rtf, ipynb, md, mhtml |
| Compressed | zip, rar, 7z, tar, gz, apk |
| Other | anything else |

**An unrecognised extension is not a reason to skip a file.** Anything that is
not in the five lists above is catalogued as *other*, with its size, its date
and its content hash, exactly like the rest. It simply has no preview and takes
no part in the stages that need to decode an image.

## What gets skipped

One short list, all of it housekeeping files that no camera or phone ever meant
as content:

| Skipped | Which files |
| --- | --- |
| By extension | `.json`, `.db`, `.thm`, `.ini`, `.nomedia`, `.part`, `.tmp` |
| By exact name | `thumbs.db`, `desktop.ini`, `.nomedia`, `.picasa.ini`, `picasa.ini`, `.ds_store` |
| By name fragment | `thumbindex`, `thumbdata`, `database_uuid`, `nomedia`, `.com.google.chrome.` |

`.thm` and `.part` are camera thumbnails and half-finished downloads. The name
fragments catch the index and housekeeping leftovers Google Photos, Picasa and
Android scatter through an export, which often have no extension at all
(`thumbdata3-123`, `nomedia_1620517712`).

`.json` is the interesting one. It is skipped as *content*, because a Google
Takeout export contains one JSON file per photo and cataloguing them would
double the size of your library with files nobody wants to browse. Those same
files are read as *metadata*, which is where most of your dates come from.

## Working out the date

This is the hard part, and it is why the stage takes as long as it does. Most
archives have lost their dates somewhere. A copy through a messaging app strips
EXIF; a restore resets every modification time to the day of the restore; a
Takeout export scatters the real timestamps into thousands of small JSON files.

Trove looks in four places and takes the first that answers.

1. **Google Takeout sidecar.** Takeout writes a JSON file per photo carrying
   the original capture time, but names it inconsistently: `IMG.jpg.json`,
   `IMG.jpg.supplemental-metadata.json`, `IMG.jpg(1).json` for `IMG(1).jpg`,
   and a truncated name for anything long. The matcher tries each of those
   rules in confidence order and falls back to a truncated-prefix match against
   the JSON files actually present in the folder. Every match records which
   rule found it.
2. **EXIF**, read with exiftool, which covers far more formats and tag dialects
   than an image library does.
3. **The filename.** Camera and app filenames carry dates in a few dozen
   recognisable shapes: `IMG_20190812_143045`, `WhatsApp Image 2020-05-03`, a
   13-digit millisecond epoch, and so on. The patterns were derived from a real
   archive rather than invented, and each returns a confidence: a full date and
   time is trusted more than a date alone.
4. **The file's own modification time**, which is the last resort and is often
   wrong.

Whichever source wins is stored alongside the date, so every date in the app
can say where it came from. The Timeline's "How dates were found" breakdown is
reading exactly that field, and it is the quickest way to see how much of your
archive is resting on a modification time.

Any date can be corrected by hand from the item view, and a correction is never
overwritten by a later scan.

**Timezones.** Takeout records times in UTC, so without a timezone set an
evening photo can roll into the next day. Setting `timezone` to an IANA zone
name converts Takeout's timestamps to local wall-clock time before storing.

## Scanning again

The walk is incremental, and safe to interrupt. Rows are written in batches, so
closing Trove mid-scan loses at most the last batch, and re-running picks up
where it stopped.

On a later scan, a file whose **size and modification time both match** what
was recorded, and which already has a content hash, is skipped without being
opened. That is the whole reason a re-scan of 150,000 files takes seconds
rather than an hour.

Three things can change, and each is handled differently:

- **The file changed** (same path, different content). Everything derived from
  the old bytes is wrong rather than stale, so its dates, metadata, faces,
  animals, fingerprints and search vectors are cleared and recomputed. One
  thing survives: a place you attached by hand, which is your judgement about
  the path rather than a fact about the bytes.
- **The file is gone.** It is marked missing rather than deleted from the
  catalogue, so its faces and names are still there if you plug the drive back
  in or restore it from a backup.
- **The file moved** within the archive. Trove sees a file gone and a new file
  arrived. Because duplicate grouping works on content, the two are recognised
  as the same photo, but per-file work is done again.

## The numbers

| Setting | Default | What it does |
| --- | --- | --- |
| `date_priority` | `takeout_json`, `exif`, `filename`, `mtime` | Sources in order; the first that produces a date wins |
| `timezone` | unset | IANA zone used to convert Takeout's UTC timestamps to local time |
| `filename_date_day_first` | true | How to read an ambiguous numeric filename date where both numbers are 12 or under |
| `fast_hash_sample_bytes` | 65536 | Head and tail sample used to decide whether a full SHA-256 is needed |

Filename dates are only accepted between 1990 and 2035. A parse outside that
range is treated as a coincidence rather than a date, because filenames are
full of long numbers that are not timestamps.

## What runs on your machine

| Component | Used for | Downloaded |
| --- | --- | --- |
| `hashlib`, `os.scandir` (Python standard library) | Content hashing and the walk | None |
| exiftool, via pyexiftool | Reading EXIF and every other metadata dialect | None |
| Pillow, with pillow-heif | Dimensions and decoding, including HEIC | None |

No model, no download. If exiftool is not installed the stage still runs and
falls back to Takeout sidecars, filenames and modification times.

## What indexing feeds

Browse and the Timeline are two views of what this stage produced. The content
hash computed here is what [Duplicates](duplicates.md) groups on. If the
archive runs [Search by description](search.md), the composer at the top of
Browse searches vectors built from the same files.

Not every stage takes every kind of file. Duplicate grouping compares all of
them by content but only fingerprints images; People and Pets look at images
and videos; Places needs GPS coordinates, whatever carries them; search by
description covers images and videos and records audio and documents as
skipped, because the model has no way to listen or read.

## What it gets wrong

**A confidently wrong sidecar match.** The truncated-prefix fallback is the
weakest rule in the matcher, and on a folder with many similarly-named long
filenames it can attach the wrong JSON to a photo. The result is a date that is
plausible and wrong.

**Ambiguous filename dates.** `03-05-2020` is either 3 May or 5 March, and
nothing in the filename says which. Where one number is over 12 the ambiguity
resolves itself; where neither is, `filename_date_day_first` decides, and it
will be wrong for every file that came from the other convention.

**Modification time as a date.** When the first three sources are silent, the
date shown is the last time the file was written, which for a restored backup
is the day of the restore. These are the files that pile up at one point in the
Timeline. The date source is recorded, so they are identifiable rather than
merely suspicious.

**Edited copies.** Google reuses the original's sidecar for its `-edited`
version, so an edit inherits the original's capture time. That is usually the
right answer and occasionally not.

**Extension, not content.** A file's kind comes from its extension alone. A
`.jpg` that is really a text file is catalogued as an image and fails to
decode later; a photo saved with no extension is catalogued as *other* and gets
no preview.
