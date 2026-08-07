---
title: Start here
summary: What Trove does to a folder of photos, and what happens after you add one.
---

Trove is a catalogue, not a photo manager. You point it at a folder you already
have and it works out what is in there: when each photo was taken, which files
are copies of each other, who and what is in them, where they were taken, and
what they show. Then it gives you a timeline, a map, a page per person, and a
search box you can type a description into.

It does all of that **in place**. Your folder is exactly as you left it.

## What Trove does to your files

Nothing. This is worth being precise about, because most tools in this space
want to reorganise your library for you.

- Nothing is **moved**. A photo stays in the folder you put it in.
- Nothing is **renamed**.
- Nothing is **edited**. No file is re-saved, re-compressed, rotated, or
  stripped of metadata.
- Nothing is **deleted**, including duplicates. Extra copies are hidden from
  browsing, and they stay on disk.
- Nothing is **written into your folder**. No sidecar files, no database, no
  hidden index directory.

Trove only ever opens your files for reading. Everything it works out is stored
in its own directory elsewhere on the machine, and you can delete that whole
directory without losing a single photo. The worst case is that Trove has to
look at the folder again.

## Adding a folder

Press **Add folder** on the start page and pick any directory. A Google Takeout
export, a phone backup, an old external drive, the messy `Pictures` folder you
have been meaning to sort out for eight years. Trove has no opinion about which.

**Folder structure does not matter, at all.** Trove walks the folder you gave
it and every folder inside it, to any depth, and catalogues every file it
finds. It does not care whether your photos are neatly filed under
`2019/summer/`, dumped in one enormous heap, or spread across forty folders
called `New Folder (3)`. It never reads folder names to guess anything, and it
never asks you to tidy up first. The structure you have is fine.

Every file gets a row, whatever its type. Photos, videos, voice notes, PDFs,
spreadsheets, ZIP files, and files with an extension Trove has never seen are
all recorded, sorted into six kinds so you can filter by them. A short list of
housekeeping files is skipped, and [Indexing](indexing.md) says exactly which.

Two things the walk deliberately does not do: it does not follow symbolic
links, so a folder linked into your archive is not catalogued twice and a link
loop cannot hang the scan; and it silently skips anything it has no permission
to read rather than stopping.

You can add as many folders as you like. Each becomes a separate archive with
its own database, its own settings and its own screens, and they never mix.
Adding the same folder twice is refused.

## Choosing what runs

When you add a folder, Trove asks what work it should do on it. Two stages are
not optional, because everything else is built on what they produce:

- **[Indexing](indexing.md)** finds every file and extracts its metadata.
- **[Duplicates](duplicates.md)** groups the copies of the same thing.

The other four are yours to choose, and the reason you are asked at all is that
some of them cost a large one-time download:

| Feature | Gives you | Downloads |
| --- | --- | --- |
| [Places](places.md) | A map of where you have been | Nothing |
| [People](people.md) | A page per person, from faces | 275 MB |
| [Pets](pets.md) | A page per animal | 35 MB |
| [Search by description](search.md) | Typing "a dog on the beach" and finding it | 689 MB |

A feature you leave switched off downloads nothing, runs nothing and shows no
section.

You can change your mind at any point, from inside the archive: **Manage
features**, at the foot of the Library health panel on the Overview, lists every
feature with what it has found here, or what it would cost to switch on.
Switching one off never deletes what it already found. Its stage stops being
scheduled and its section disappears, and switching it back on picks up where it
left off.

## What happens next

Nothing to press. As soon as the archive exists the pipeline starts, and it
keeps itself up to date from then on. You can browse while it works.

The stages run in the order below because each reads what the ones before it
produced. Scanning, dating and search indexing run at the same time as each
other; duplicate grouping, places, and people and pets take turns, because each
of those rewrites the catalogue wholesale.

| Stage | Produces | What later stages use it for |
| --- | --- | --- |
| [Indexing](indexing.md) | One row per file, with a date | Everything |
| [Duplicates](duplicates.md) | Groups of copies, one kept | Not processing the same photo eight times |
| [Places](places.md) | Coordinates, grouped into places | Nothing |
| [People](people.md) | Faces, grouped into people | Nothing |
| [Pets](pets.md) | Animals, grouped into individuals | Vetoing animal faces in People |
| [Search by description](search.md) | One vector per photo | Answering a typed description |

The **Library health** panel on the Overview shows what each stage is doing,
what it is waiting on and how much is left. There is a **Pause all** button
there when you want the machine back.

A first run on a large archive takes hours. It is resumable: closing Trove
mid-scan loses nothing, and reopening picks up where it stopped.

**It keeps up on its own after that.** Add photos to the folder, delete some,
reorganise it however you like, using whatever program you normally use. While
the archive is open Trove checks the file count every minute or so and catches
up by itself, looking only at what actually changed. There is no re-scan button
and nothing to remember. [Indexing](indexing.md) describes exactly what happens
to a file that appears, disappears, moves or changes.

## Two things that are true of every stage

**Nothing leaves this computer.** Every model on these pages runs locally, on
your own processor. There is no account, no API key and no request to a server.
The only outbound traffic Trove ever makes is fetching model weights once, and
the map's optional street-map layer. [Privacy and data](privacy.md) covers it
exactly.

**Nothing is destroyed.** Duplicate copies are hidden, not deleted.
Re-clustering rebuilds people from scratch but carries your names and
corrections across it. Switching a feature off leaves everything it found.

## How to read the numbers

Most pages here carry a figure like this one, showing where a threshold sits
relative to the values it has to separate.

```scale
range 0 1
band 0 0.30 muted Different people
band 0.75 0.97 The same person
mark 0.55 faces_centroid_merge_sim
note Cosine similarity between cluster centroids, measured on one real archive. The gap between the two bands is the margin the threshold has to work with.
```

The empty space between the bands is the part worth looking at. A threshold
with a wide gap either side is one you will rarely notice; a narrow gap is
where mistakes come from, and each page says which of its numbers are which.

The name under the mark is the real setting name. Every one of them lives in
`config.json` in Trove's data directory, and a value there overrides the
default shown here. You do not need to touch any of them.

## Where to go next

- [Indexing](indexing.md), for the detail on which files are catalogued and how
  dates are worked out.
- The page for whichever screen you are looking at. Every section in the app
  has a **How this works** button that opens it.
- [Common questions](faq.md), for the practical ones: how long it takes, what
  happens when you add or delete files, how to back it up, and how to start
  over.
