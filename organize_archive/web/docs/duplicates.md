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
a 150,000-photo archive. Instead the hashes go into a BK-tree, a structure that
uses the triangle inequality of Hamming distance to skip whole branches that
cannot contain a match, turning each lookup into a few dozen comparisons.

Exact matches and visual matches then go into one union-find structure
together, so a photo that is byte-identical to one file and visually identical
to another ends up in a single group rather than two.

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

Perceptual hashing only runs on images. Videos, audio and documents are grouped
by exact content only, so two re-encodes of the same clip are two files as far
as Trove is concerned.

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
edited. Everything in the group is still there, so this is recoverable rather
than permanent.

## When it runs

Grouping is rebuilt from scratch each time, over the whole archive, and it is
scheduled automatically, so there is nothing to start. A run that is
interrupted leaves the previous grouping completely intact: the old groups are
not cleared until the fingerprints have been computed, and the whole rebuild
is one transaction, so a crash halfway through can never leave the archive with its
duplicates briefly un-hidden.
