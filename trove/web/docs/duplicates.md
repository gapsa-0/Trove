---
title: Duplicates
summary: Byte-identical copies and re-saved exports of the same shot, grouped.
feature: duplicates
---

The same photo arrives more than once. A Google Takeout export and a phone
backup of the same camera roll; a picture sent through WhatsApp and saved again;
an old drive copied into a new folder "just in case". Trove groups those copies
together, keeps one of them visible, and hides the rest.

Hidden is not deleted. Every copy is still on disk, still in its original
folder, and still listed on this archive's Duplicates screen. Nothing here ever
removes a file.

## How it works

Two files can be the same in two different ways, and Trove checks for both.

**Identical bytes.** Every file gets a SHA-256 of its full contents during
[indexing](indexing.md). Two files with the same digest are the same file, with
no judgement involved. Before that full hash is computed, a cheap fingerprint
(the file's size plus its first and last 64 KB) filters out the files that
cannot possibly match, so the expensive read only happens where it might pay.

**The same picture, saved differently.** A JPG re-compressed by a messaging app
has completely different bytes from the PNG it came from, and no hash will ever
connect them. So every image also gets a *perceptual hash*: the picture is
reduced to a small greyscale matrix, run through a discrete cosine transform,
and the low-frequency coefficients are compared against their median to give 64
bits. Two pictures that look the same to a person produce hashes that differ in
only a few of those bits, however differently they were encoded.

EXIF orientation is applied before hashing, so a photo stored sideways and the
same photo stored upright fingerprint identically instead of looking like two
unrelated pictures.

Comparing every hash against every other one would be 11 billion comparisons on
a 150,000-photo archive. Instead each 64-bit hash is cut into seven bands, and
every band is indexed. Two hashes that differ in at most six bits can differ in
at most six bands, so at least one of the seven must match *exactly* — looking
up all seven bands finds every genuine match while reading almost none of the
archive.

Exact matches and visual matches then go into one union-find structure
together, so a photo that is byte-identical to one file and visually identical
to another ends up in a single group rather than two.

## Why a second run is quick

Whether two pictures look alike is a fact about the pictures, not about when
Trove last asked. So the pairs it finds are written down, against the exact
content they were found for. Add twenty photos to an archive of ninety thousand
and only those twenty are compared against the rest; delete a few and nothing is
compared at all. A photo that gets edited or re-saved has its old pairs thrown
out and is compared afresh, because its content — and so its fingerprint — is no
longer the one those pairs were about.

Changing `phash_hamming_threshold` discards every stored pair, since they were
found under the old setting. The next run searches the whole archive again.

The groups themselves are still rebuilt from scratch every single run. That is
deliberate: it means a grouping damaged by a crash, or left behind by an older
version, is repaired by the next pass rather than persisting, and it costs
seconds now that the search behind it is not repeated.

## The numbers

```scale
range 0 64
band 0 6 Grouped as the same photo
mark 6 phash_hamming_threshold
note Hamming distance between two 64-bit perceptual hashes: how many of the 64 bits differ. This band is the rule, not a measurement: 0 is a bit-for-bit identical fingerprint and 64 is the opposite of one.
```

| Setting | Default | What it does |
| --- | --- | --- |
| `phash_hamming_threshold` | 6 | Two images are the same shot when at most this many of their 64 hash bits differ |
| `fast_hash_sample_bytes` | 65536 | Head and tail sample size for the prefilter that decides whether a full SHA-256 is worth computing |

Perceptual hashing only runs on still images. Videos, audio and documents are
grouped by exact content only, so two re-encodes of the same clip are two files
as far as Trove is concerned.

**Animated files are on that side of the line too.** A fingerprint can only
describe one frame, and for an animation that frame is the first one, which
says how the animation opens, not what it is. Two unrelated GIFs that share a
title card, or both fade in from white, come out with the same fingerprint.
So an animated file is never fingerprinted, and is grouped by its bytes alone.
This is decided by looking inside the file rather than at its name, so a GIF
saved as `.png` is treated as the animation it is.

## What you see on the Duplicates screen

Four numbers across the top: how many **unique files** the archive holds, how
many **duplicate groups** were found, how many **redundant copies** those groups
contain, and how much space you would get back by removing them.

**Unique files** is the archive counted with each group collapsed to the one
copy Trove kept, not the files that turned out to have no duplicates. A photo
sitting on four hidden copies is one unique file. Before duplicate detection
has ever run nothing is hidden, so the number is simply every file you have.

Below that, a breakdown of what the copies actually are, split two ways: by
match, separating byte-identical copies from visual matches, and by media kind.
The distinction matters. An identical copy is safe and boring. A visual match
is a judgement, and it is the one worth glancing at.

A third bar, marked *unique*, splits the unique files themselves by kind, so
you can read a hundred redundant videos against how many videos you actually
have. It is a share of **unique files**, not of the redundant copies above it,
which is why it sits below a rule: the segments of the two do not compare.

Then one row per group, biggest saving first. Each row shows every copy with
its file name and the folder it lives in, and the one Trove keeps wears a
filled *✓ Kept* tag. Click any copy to open it; the viewer's arrows then step
through that group and stop at its edges, so a comparison never wanders into
the next group's photos.

The folder under a copy is shortened to its last two parts, because that is
where copies of one file differ — `…/Bariloche - dia 1` beside
`…/Bariloche - dia 2`. Hovering a copy shows the whole path.

Two controls sit above the list. The first narrows it to groups holding at
least one identical copy, or at least one visual match, and a group can answer
to both, since a group matched visually often also holds a byte-identical copy.
The second orders by how many copies a group holds, either way, in place of the
biggest-saving order. When a filter is on, the count beside the controls says
how much of the archive is being shown.

**Trove will not delete them for you.** There is no delete button, here or
anywhere else. The screen exists to tell you what is redundant and where it
lives, so you can act on it with your file manager if you want to.

## Copies, from the file you are looking at

You do not have to come to this screen to find out whether a file has copies.
Open any file and its panel has a **Duplicates** section: the whole group as
thumbnails, tagged the same way as here (kept, identical, looks the same)
with the copy you are looking at marked. Pressing another copy opens it, with
the arrows kept inside the group and a way back to the copy you came from.

A file that has been compared and matched nothing says *No duplicates found*. A
file the last grouping run has not reached, scanned since or not hashed yet,
says it has not been compared instead, because that is a different fact.

## Which copy is kept

One member of each group is the **canonical** copy: the one that stays visible
in Browse, in the Timeline, and everywhere else. The choice is made by working
down this list until one file wins, so it is the same on every re-run.

1. Most pixels: width × height, so a full-resolution original beats a
   thumbnail
2. Largest file: at equal dimensions, the least re-compressed copy
3. Has a Google Takeout sidecar, which is richer provenance
4. Has a resolved date
5. Earliest resolved date
6. Path, then database id, a tie-break that is stable rather than meaningful

The other members are marked hidden. They keep their row, their metadata, and
their place in the group.

## What runs on your machine

No neural network is involved in this stage at all. It is hashing and tree
search.

| Component | Used for | Downloaded |
| --- | --- | --- |
| `hashlib` (Python standard library) | SHA-256 content hashes | None |
| Pillow, with pillow-heif | Decoding images, including HEIC | None |
| ImageHash | 64-bit perceptual hash | None |

Perceptual matching needs Pillow and ImageHash to be installed. Without them,
exact duplicate detection still works and the visual pass is skipped rather
than failing.

## What it gets wrong

**Burst shots and near-identical frames.** Two frames taken a fraction of a
second apart can land within 6 bits of each other and be grouped as one photo,
even though they are two different pictures. Raising
`phash_hamming_threshold` makes this worse; lowering it starts missing genuine
re-compressions.

**Heavy crops and edits.** A cropped or filtered version of a photo is a
different picture to the perceptual hash, and will not be grouped with its
original. That is usually what you want, but it means "I have this twice" is
not always caught.

**Flat, low-detail images.** Screenshots of mostly-white pages, blank scans and
solid-colour images have very little in their low-frequency coefficients to
tell them apart, so unrelated ones can collide. If a group looks wrong, it is
most likely one of these.

**Which copy got kept.** The rule above prefers the largest original, which is
usually right, but it has no way to know that the smaller file is the one you
edited. Nothing is lost either way — every copy is still on disk, still listed
in its group here, and still openable from this screen. What Trove cannot do
yet is be told it picked wrong: there is no way to promote another copy to the
kept one, so the copy you wanted stays out of Browse until the file itself
changes.

## When it runs

Grouping is rebuilt from scratch each time, over the whole archive, and it is
scheduled automatically, so there is nothing to start. A run that is
interrupted leaves the previous grouping completely intact: the old groups are
not cleared until the fingerprints have been computed, and the whole rebuild
is one transaction, so a crash halfway through can never leave the archive with its
duplicates briefly un-hidden.
