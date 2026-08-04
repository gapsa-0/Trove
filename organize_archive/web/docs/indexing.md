---
title: Indexing
summary: Walking the folder, and working out when each file was actually taken.
feature: index
---

Indexing is the stage that makes the archive exist. It walks the folder you
added, records one row for every photo, video, audio file and document it
finds, and resolves a date for each one. Everything else on these pages is
built on what it produces.

It reads. It does not write to your folder: nothing is moved, renamed, edited,
or converted, and the only thing Trove ever stores is its own database in its
own data directory.

## How it works

**The walk.** Every file under the folder is visited once. Ignore rules skip
the things that are never worth cataloguing: system files, thumbnail caches,
and a list of extensions that hold no media. What remains is classified by
type: image, video, audio, document, compressed archive, or other.

The walk is incremental and safe to interrupt. A file whose size and
modification time are unchanged since the last pass, and which already has a
content hash, is skipped without being read again, which is what makes a
re-scan of 150,000 files take seconds rather than an hour. Rows are written in
batches, and re-running after a crash picks up where it stopped.

**The date.** This is the hard part, and it is why the stage takes as long as
it does. Most archives have lost their dates somewhere: a copy through a
messaging app strips EXIF, a restore resets every modification time to the day
of the restore, and a Takeout export scatters the real timestamps into
thousands of small JSON files. Trove looks in four places and takes the first
that answers.

1. **Google Takeout sidecar.** Takeout writes a JSON file per photo carrying
   the original capture time, but names it inconsistently: `IMG.jpg.json`,
   `IMG.jpg.supplemental-metadata.json`, `IMG.jpg(1).json` for `IMG(1).jpg`,
   and a truncated name for anything long. The matcher tries each of those
   rules in confidence order and falls back to a truncated-prefix match against
   the JSON files actually present in the folder. Every match records which
   rule found it.
2. **EXIF**, read with exiftool, which covers far more formats and tag
   dialects than an image library does.
3. **The filename.** Camera and app filenames carry dates in a few dozen
   recognisable shapes: `IMG_20190812_143045`, `WhatsApp Image 2020-05-03`,
   a 13-digit millisecond epoch, and so on. The patterns here were derived from
   a real archive rather than invented, and each returns a confidence: a full
   date and time is trusted more than a date alone.
4. **The file's own modification time**, which is the last resort and is often
   wrong.

Whichever source wins is stored alongside the date, so every date in the app
can say where it came from. The Timeline's date-source breakdown is reading
exactly that field.

**What indexing feeds.** Browse and the Timeline are two views of what this
stage produced. The content hash it computes here is also what
[Duplicates](duplicates.md) groups on, and if the archive runs
[Search by description](search.md), the composer at the top of Browse is
searching vectors built during the same pass.

**Timezones.** Takeout records times in UTC. Without a timezone set, an evening
photo can roll into the next day. Setting `timezone` to an IANA zone name
converts Takeout's timestamps to local wall-clock time before they are stored.

## The numbers

| Setting | Default | What it does |
| --- | --- | --- |
| `date_priority` | `takeout_json`, `exif`, `filename`, `mtime` | Sources in order; the first one that produces a date wins |
| `timezone` | unset | IANA zone used to convert Takeout's UTC timestamps to local time |
| `filename_date_day_first` | true | How to read an ambiguous numeric filename date where both numbers are 12 or under |
| `fast_hash_sample_bytes` | 65536 | Head and tail sample used to decide whether a full SHA-256 is needed |

Filename dates are only accepted between 1990 and 2035. A parse outside that
range is treated as a coincidence rather than a date, because filenames contain
plenty of long numbers that are not timestamps.

## What runs on your machine

| Component | Used for | Downloaded |
| --- | --- | --- |
| `hashlib`, `os.walk` (Python standard library) | Content hashing and the walk | None |
| exiftool, via pyexiftool | Reading EXIF and every other metadata dialect | None |
| Pillow, with pillow-heif | Dimensions and decoding, including HEIC | None |

No model, no download. If exiftool is not installed the stage still runs and
falls back to Takeout sidecars, filenames and modification times.

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
merely suspicious, and any date can be corrected by hand.

**Edited copies.** Google reuses the original's sidecar for its `-edited`
version, so an edit inherits the original's capture time. That is usually the
right answer and occasionally not.
