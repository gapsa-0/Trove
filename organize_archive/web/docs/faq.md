---
title: Common questions
summary: The practical ones: time, disk, moving files, backups, and starting over.
---

Answers to the things people ask before and after adding their first folder. If
you are here before adding one, [Start here](index.md) is the better page.

## Will Trove touch my photos?

No. It opens files for reading and never writes into your folder. Nothing is
moved, renamed, edited, re-compressed or deleted, and no sidecar or index file
is left behind. Everything Trove works out lives in its own directory somewhere
else on the machine.

## How long does the first run take?

Hours, for a large archive. There is no useful number to give, because what
dominates depends on your files: hashing every byte is limited by your disk,
dating files is limited by how many have Takeout sidecars to match, and the
People, Pets and search stages are limited by your processor because they
decode and run a model over every image.

You can browse while it works, and everything appears progressively rather than
all at the end. The Overview's **Library health** panel shows what is running
and what is left.

## Can I stop it and come back?

Yes, at any point. Every stage is resumable, and closing Trove mid-run loses at
most a few seconds of work. Reopening the archive picks up where it stopped.

There is a **Pause all** button on the Overview, and each stage has its own
pause control next to it if you want to stop just one. Pausing is remembered
across restarts, so a stage you paused stays paused until you resume it.

## Why does my archive show fewer files than the folder has?

Three likely reasons, in order:

1. **The scan is still going.** Check the Overview.
2. **Duplicates are hidden.** Browse shows one copy of each thing. The
   Duplicates screen shows every group and the counts include them. Trove
   deletes nothing.
3. **Housekeeping files are skipped.** `Thumbs.db`, `.DS_Store`, Picasa index
   files, Takeout's `.json` sidecars and a few others are not catalogued as
   content. [Indexing](indexing.md) lists them all.

## Do I need to organise my folders first?

No, and it makes no difference if you do. Trove walks every subfolder to any
depth and never reads folder names to guess anything. A single heap of 80,000
files works exactly as well as a tidy year-by-month tree.

## What happens if I move, rename or delete files afterwards?

Trove notices on the next scan.

- **Deleted or on a disconnected drive:** the file is marked missing, not
  removed from the catalogue. Its faces, names and dates are still there if it
  comes back.
- **Moved or renamed inside the archive:** Trove sees one file gone and one
  arrived. Because [Duplicates](duplicates.md) works on content, it recognises
  the same photo, but the per-file work is done again.
- **Edited in place:** everything derived from the old bytes is cleared and
  recomputed, since it is now wrong rather than merely old. A place you
  attached by hand survives.

## Can I add a folder that is inside another archive?

Trove only refuses the exact same folder twice, so nesting is allowed, but it
is rarely what you want: the files inside get catalogued independently in both
archives, with separate people, separate places and separate duplicate groups.
Prefer one archive per real collection.

## Does it need an internet connection?

Only once, and only if you enable a feature with a model behind it. People,
Pets and Search by description fetch their weights the first time they run, and
after that everything works fully offline. Indexing, Duplicates and Places
download nothing at all, so an archive with only those never touches the
network.

The map's street-map layer is a separate, optional exception, and it is the
only outbound call that depends on your own data. [Privacy and
data](privacy.md) has the full list.

## Where does Trove keep its data?

| Platform | Location |
| --- | --- |
| Linux | `~/.local/share/organize_archive` |
| Windows | `%LOCALAPPDATA%\organize_archive` |
| macOS | `~/Library/Application Support/organize_archive` |

Inside it, each archive is fully isolated in `archives/<id>/`, with its own
`archive.db` and its own thumbnail and crop cache. Shared across all of them
are `config.json` and the downloaded models, which are large and worth keeping.

The directory is still called `organize_archive` rather than `Trove`. The
product name changed; the data path deliberately did not, so catalogues built
by older versions keep working.

## How do I back it up?

Close Trove and copy that whole directory. To restore, close Trove and put it
back.

It is worth doing, because it holds work you cannot get back automatically: the
names you typed, the merges and corrections you made, and the places you named.
Everything else in there is derived and would be rebuilt by a re-scan.

Your media folder is a separate matter, and Trove is not a backup tool for it.

## How much disk does it use?

The catalogue itself is small. The cache is not: it holds a thumbnail for every
image and video, plus a crop for every face and animal found, so it grows with
the number of files rather than their size. The models are the largest single
item, up to about 1 GB shared across every archive.

Removing an archive deletes its database and cache and frees all of that. The
models stay, since another archive may want them.

## How do I start over on one archive?

Remove it from the start page and add the folder again. That deletes its
database and cache and nothing else. Your media folder is untouched, and so is
every other archive.

Removing an archive does discard the names and corrections you made in it, so
back up the data directory first if you want them.

## Why has one person been split into two pages?

Because splitting is the mistake Trove prefers to make. Every threshold in
[People](people.md) is set to prefer two pages for one person over one page for
two people, since merging is one drag and unpicking a wrong merge is not. Big
gaps in age, glasses, and consistent profile-only shots each tend to produce a
second group. Drag one onto the other.

## Why does search by description find nothing?

Usually because the archive genuinely holds nothing like what you typed, and
that is deliberate: a floor silences a query the archive cannot answer, since a
page of unrelated photos is a worse answer than an empty one.

If you expected a match, the [Search by description](search.md) page has the
detail. Two common causes: it describes what is visible rather than reading
text in a picture, and it has no idea who anyone is, which is what
[People](people.md) is for.

## Can I delete the duplicates it found?

Not from Trove, which never deletes anything. The Duplicates screen shows each
group, which copy it would keep, and how much space the rest occupy, so you can
act on that with your file manager if you want to. Every copy is listed with
its folder.

## Can I run it without the window?

Yes. The `oa` command does the same work from a terminal: `oa scan`, `oa
enrich`, `oa dedup`, `oa faces`, `oa pets`, `oa status`. `oa gui` opens the
window. All of them are resumable and safe to interrupt.

## Something went wrong. Where are the logs?

`oa logs` prints the last 200 lines, and `oa logs --path` prints where the file
lives. It rotates at 5 MB and keeps three older files, so it never needs
clearing by hand.

Nothing in the log is ever sent anywhere. It stays on this machine and is only
useful if you choose to attach it to a bug report.
