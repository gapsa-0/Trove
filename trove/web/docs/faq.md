---
title: Common questions
summary: The practical ones: time, adding and deleting files, disk, backups, starting over.
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

## I added new photos to the folder. Do I need to do anything?

No. Drop them in and carry on.

While the archive is open, Trove counts the files under it every minute or so,
using a cheap directory listing that opens nothing. When the count stops
matching the catalogue it scans, and it only looks at what is new: adding fifty
photos to a folder of 150,000 costs the fifty. Their dates, faces, places and
search vectors follow on their own.

If Trove is closed, or that archive is not the one open, nothing happens in the
background and the new files are picked up next time you open it. There is no
re-scan button because there is nothing for it to do.

## What if I delete files from the folder?

Also fine, and you do not need to tell Trove either. It notices the count fall
and marks those rows **missing** rather than deleting them.

A missing file stops appearing in Browse, on the Timeline, in People, Pets,
Places and search results, and its duplicate group closes up around it. Keeping
the row is what lets an unplugged drive or a restored backup bring everything
back exactly as it was, names and corrections included.

Deleting from your folder is always safe. Trove never re-creates a file and has
no opinion about what you remove.

## What if I move, rename or edit files?

- **Moved or renamed inside the archive:** Trove sees one file gone and one
  arrived, so the old row goes missing and the new one is catalogued fresh.
  Because [Duplicates](duplicates.md) works on content rather than paths, the
  two are still recognised as the same photo.
- **Edited in place:** everything derived from the old bytes is cleared and
  recomputed, since it is now wrong rather than merely old. A place you
  attached by hand survives, because that is your judgement about the file
  rather than a fact about its contents.
- **Moved out of the archive entirely:** the same as deleting it.

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
| Linux | `~/.local/share/trove` |
| Windows | `%LOCALAPPDATA%\trove` |
| macOS | `~/Library/Application Support/trove` |

Inside it, each archive is fully isolated in `archives/<id>/`, with its own
`archive.db` and its own thumbnail and crop cache. Shared across all of them
are `config.json` and the downloaded models, which are large and worth keeping.

This folder used to be called `organize_archive`. If you are upgrading from a
version that used that name, Trove moves the folder and updates the paths
recorded inside it the first time it starts, so your catalogue carries over
untouched and nothing is scanned again.

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

Yes. The `trove` command does the same work from a terminal: `trove scan`, `trove
enrich`, `trove dedup`, `trove faces`, `trove pets`, `trove status`. `trove gui` opens the
window. All of them are resumable and safe to interrupt.

## Something went wrong. Where are the logs?

`trove logs` prints the last 200 lines, and `trove logs --path` prints where the file
lives. It rotates at 5 MB and keeps three older files, so it never needs
clearing by hand.

Nothing in the log is ever sent anywhere. It stays on this machine and is only
useful if you choose to attach it to a bug report.
