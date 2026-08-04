---
title: How Trove works
summary: The whole pipeline, in the order it runs, and what each stage decides.
---

Trove reads a folder you already have and works out what is in it: when each
photo was taken, which of them are copies of each other, who and what is in
them, where they were taken, and what they show. It does that with seven
stages, on your own machine, and it never moves, renames, or edits a file.

These pages describe each stage: the models it loads, the algorithm it runs,
and the exact numbers it decides by. They exist because most of what Trove does
is a judgement call: two photos are "the same shot", two faces are "the same
person". A judgement call you cannot inspect is one you cannot trust.

## The chain

Each stage reads what the ones before it produced, which is why they run in
this order and not another.

| Stage | Produces | Everything downstream uses it for |
| --- | --- | --- |
| [Indexing](indexing.md) | One row per file, with a date | Knowing the archive exists at all |
| [Duplicates](duplicates.md) | Groups of copies, one kept | Not processing the same photo eight times |
| [People](people.md) | Faces, grouped into people | Nothing |
| [Pets](pets.md) | Animals, grouped into individuals | Vetoing animal faces in People |
| [Places](places.md) | Coordinates, grouped into places | Nothing |
| [Search by description](search.md) | One vector per photo | Answering a typed description |

Indexing and Duplicates always run, and everything else is built on what
they produce. The other four are chosen per archive, and an archive that declines
one never downloads its models, never schedules its work, and never shows its
section. You can change that later without losing anything already found.

## Two things that are true of every stage

**Nothing leaves this computer.** Every model listed on these pages runs
locally, through onnxruntime, on weights stored in your own cache directory.
There is no account, no API key, and no request to a server. [Privacy and
data](privacy.md) covers what that means in detail.

**Nothing is destroyed.** Duplicate copies are hidden, not deleted. A
re-clustering rebuilds people from scratch but carries your names, your
corrections, and your manual tags across it. Switching a feature off leaves
everything it already found in place.

## How to read the numbers

Most pages carry a figure like this one, showing where a threshold sits
relative to the values it has to separate.

```scale
range 0 1
band 0 0.30 muted Different people
band 0.75 0.97 The same person
mark 0.55 faces_centroid_merge_sim
note Cosine similarity between cluster centroids, measured on one real archive. The gap between the two bands is the margin the threshold has to work with.
```

The empty space between the bands is the part worth looking at. A threshold
with a wide gap on both sides is one you will rarely notice; a threshold with a
narrow gap is where mistakes come from, and each page says which of its numbers
are which.

The name under the mark is the real setting name. Every one of them is stored
in `config.json` in your data directory, and the value there overrides the
default shown here.
