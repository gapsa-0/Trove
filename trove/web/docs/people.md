---
title: People
summary: Finding faces, judging whether they are usable, and grouping them into people.
feature: people
---

{{tagline}}. Trove finds every face in the archive, measures whether each one
is good enough to identify anyone from, and groups the usable ones into people
you can name, correct, merge and split.

This is the stage that has the most ways to be wrong, so it is built around one
idea: a bad face is not allowed to influence anything. That single restriction
explains most of what follows.

## How it works

**Detection.** Each image is decoded once and passed to SCRFD (`det_10g`), a
face detector from InsightFace's buffalo_l pack. It returns a box, a
confidence, and five landmarks (eyes, nose, mouth corners) per face. Videos
are covered by sampling a handful of keyframes rather than decoding every frame.

**Alignment.** The five landmarks drive a similarity transform that warps each
face onto the standard 112×112 template, so a face turned or tilted in the
original arrives at the embedder the same way every other face does. Comparing
un-aligned faces mostly measures head pose, not identity.

**Embedding.** The aligned crop goes through AdaFace ir101, trained on
WebFace12M, producing a 512-dimensional vector. Two vectors of the same person
point in a similar direction; cosine similarity between them is the only
identity signal used anywhere in this stage.

**The quality gate.** AdaFace's output has a length as well as a direction, and
the paper establishes that length, the feature norm, as a proxy for how good
the face image is. It costs nothing to use, because the embedder computes it
anyway. The raw norm is a model-specific magnitude, so it is mapped onto 0–1
against *this archive's own* distribution of norms, fixed once from a sample and
stored, so a face's tier never depends on when it happened to be scanned.

Every face lands in one of three tiers, and the tier decides what it is allowed
to do:

```scale
range 0 1
band 0 0.55 soft Borderline: may join a person, never start one
band 0.55 1 High: may start a person
mark 0.55 faces_fiqa_high
note Face image quality, from the AdaFace feature norm, calibrated against this archive's own distribution. Measured on one archive: about half of all faces reach high.
```

**The third tier is measured differently, on purpose.** Whether a face is good
enough to *start* a person is a comparative question, and the calibrated 0–1
score answers it. Whether a face is unusable is not: that is true or false about
the face itself, and must not depend on what else you photographed. So the
discard line is drawn on the raw feature norm instead, at
`faces_fiqa_floor_norm`, and a face above it is never thrown away however low it
scored. Discarded faces are excluded from clustering and hidden in the app —
never deleted, so moving the line puts them back without re-reading a pixel.

The earlier design drew this line on the score too, and that was a mistake worth
recording: a fixed cut on a score derived from the archive's own mean and spread
discards a fixed share of *every* archive, however good it is. On one archive it
sat at 1.28 standard deviations below the mean and threw away the bottom 10.5%
of faces regardless of what they looked like — sharp, frontal, well-lit portraits
among them, and on 754 photos the only face found, which then reported no people
at all. The floor cuts the same archive to 2.6%.

**Clustering, in two passes.** Only HIGH faces take part in the first pass, so
every group it builds is pure by construction. Those groups are built in three
stages:

1. **Mutual k-NN fragments.** Two faces are linked only when each is among the
   other's 5 most similar faces *and* their cosine similarity is at least 0.75.
   Requiring reciprocity is the important half: a plain similarity threshold is
   single-linkage, and one blurry face weakly similar to two different people
   fuses both of them. On a real archive that produced a single blob holding
   about 40% of all faces, and raising the threshold did not fix it. It just
   started splitting real identities as well.
2. **Average-linkage merge.** Fragment centroids are merged while their mean
   cross-pair similarity is at least 0.40. Complete linkage was tried first and
   rejected: it requires *every* cross pair to be close, so the same person
   young and old never coalesces, and the most-photographed person split into
   about thirty clusters.
3. **Centroid merge.** Stage 2's mean-cross-pair metric is dragged down by a
   loose cluster's own spread, so a tight cluster and a loose one of the same
   person can survive as two people. Comparing centroid *directions* divides
   that spread out and rejoins them.

The second pass then offers each BORDERLINE face to the finished groups. It may
attach to one; it may never create one and never merge two. Since the damage a
bad face does is always a merge, and a borderline face cannot cause a merge,
bridge faces stop mattering.

A group needs at least 3 faces to become a person. Everything below that stays
unassigned rather than becoming a person with one photo.

```scale
range 0 1
band 0 0.30 muted Different people
band 0.75 0.97 The same person
mark 0.40 faces_merge_sim
mark 0.75 faces_core_link_sim
note Cosine similarity between AdaFace embeddings, measured on one real archive. The wide gap is why merging can sit as low as 0.40 without fusing distinct people. The centroid merge at 0.55, in between, still has a quarter of the scale of clearance below it.
```

## The numbers

| Setting | Default | What it does |
| --- | --- | --- |
| `faces_det_size` | 640 | Detector input square; larger finds smaller faces and runs slower |
| `faces_min_score` | 0.50 | Minimum detector confidence for a face to be kept |
| `faces_min_px` | 50 | Minimum box side in original pixels; below this there is too little detail to trust |
| `faces_max_clipped_fraction` | 0.18 | Reject a box mostly outside the frame |
| `faces_fiqa_floor_norm` | 16.0 | Raw feature norm below which a face is excluded from clustering and hidden |
| `faces_fiqa_low` | 0.18 | The same line for faces scored without a feature norm, on the 0–1 scale |
| `faces_fiqa_high` | 0.55 | At or above this a face may seed a group |
| `faces_fiqa_h` | 2.0 | Half-width, in standard deviations, of the norm-to-score mapping |
| `faces_fiqa_calib_sample` | 2000 | Faces used once to fix the archive's mean and spread |
| `faces_knn_k` | 5 | Neighbours each face is capped to in the first pass |
| `faces_core_link_sim` | 0.75 | Similarity floor for linking two faces into a fragment |
| `faces_merge_sim` | 0.40 | Mean cross-pair similarity for merging two fragments |
| `faces_centroid_merge_sim` | 0.55 | Centroid-direction similarity for merging two finished groups |
| `faces_border_assign_sim` | 0.55 | Similarity at which a borderline face joins a group |
| `faces_border_votes` | 3 | Members a borderline face is compared against, rather than the centroid alone |
| `faces_min_faces` | 3 | Faces a group needs before it becomes a person |
| `detect_video_frames` | 5 | Keyframes sampled per video; 0 disables video detection |
| `detect_video_same_face` | 0.55 | Similarity above which two faces in one video are collapsed to one row |

## What runs on your machine

| Model | Role | Size |
| --- | --- | --- |
| SCRFD `det_10g` (InsightFace buffalo_l) | Face detection and landmarks | part of {{download_mb}} MB |
| AdaFace ir101 / WebFace12M | 512-d identity embedding, and the quality score | part of {{download_mb}} MB |

Both run through onnxruntime on the CPU. The weights download once, into your
cache directory, and are shared by every archive that uses this feature. No
photo and no vector is ever sent anywhere.

Enabling [Pets](pets.md) alongside People costs almost nothing extra: the two
share a single image decode, and the animal detector is what lets an animal
face be recognised as one.

## What you can do on the People screen

**Name someone.** Click the name under their photo and type. That name is what
survives every later re-clustering.

**Take a name back off.** The same editor offers "Remove name", which returns
the group to being an unnamed cluster. It also releases any face you had moved
onto that person by hand: those moves are remembered by name, so leaving them
in place would rebuild the person, under the name you just removed, at the next
clustering pass. Undoing the removal from the history restores both.

**Merge two people.** Drag one card onto another when the same person has ended
up with two pages. Trove asks first, and the merge can be undone.

**Or merge by name.** Every card's ⋯ menu, and every person's own page, offers
"Merge with…" and a list of the people you have already named. Dragging needs
both cards on screen; this is for the other case, where the group you
recognise is hundreds of cards away.

**Fix one photo.** Each photo on a person's page carries two controls of its
own. *This is not the person* takes the photo off them and stops it being
suggested for them again. *Make cover photo* chooses which face represents them
on their card — Trove otherwise picks whichever one it judged sharpest, which
is not always the one that looks like them. Your choice outranks that from then
on, including after re-clustering.

**Take a group off the screen.** Each card, and each person's page, offers two
ways out, and they are not the same thing. *Not a person* is about the
detections — a doll, a statue, a face on a poster — and takes them out of
grouping for good. *Unknown person* is about the list: a real person you would
rather not see on it, whose faces go on grouping exactly as before. Hidden
groups collect at the foot of the People screen and come back with one click.

**See what you changed.** A person's page keeps a record of the edits made to
it — merges, names, photos added or dropped by hand — behind the clock in its
top bar, most recent first, each one offering to undo itself. Undoing does not
erase the entry; it marks it, because what you did is as much a part of the
record as what you took back. Pets keep the same record.

**Answer "Same person?"** At the top of the screen Trove offers pairs it is
unsure about, with how similar it thinks they are. Answering either way is
remembered as a rule, not just applied once: a "no" stops those two being
merged by any future run.

**Move one face.** Open a photo and reassign a face that was put with the wrong
person, or tag someone the detector missed entirely.

**Hide someone.** Strangers in the background of holiday photos get pages too.
Hiding one takes it off the screen without deleting anything.

## What your corrections do

Clustering is rebuilt from scratch on every run, which would normally throw
away everything you had fixed. Three things are carried across it:

- **Names**, matched to the new groups by which faces they share.
- **Manual tags**, meaning "this face is Mari", re-applied afterwards and
  anchored to the name rather than to a group that no longer exists.
- **Review answers**, meaning "these two are the same" or "these two are not",
  folded back in as constraints and anchored to face ids, which do survive a
  rebuild.

## What it gets wrong

**One person split into several.** This is the common failure, and it is
deliberate. Every threshold above is set to prefer splitting a person over
merging two, because merging two people is the mistake that is annoying to
undo. Large gaps in age, heavy glasses, and consistent profile-only shots each
tend to produce a second group. Merging them by hand takes one drag.

**Faces that are not people.** Dolls, statues, cake figurines, portraits within
photos, and faces on packaging are all real faces to a detector. The animal
cross-check removes cats and dogs; nothing removes a mannequin.

**Small and blurry faces disappear.** A face under 50 pixels, or one whose
feature norm is under `faces_fiqa_floor_norm`, is excluded entirely rather than
clustered badly. Someone who only ever appears in the background of group shots
may not get a page at all.

**Look-alikes.** Close relatives at similar ages sit closer together than the
0.30 that separates typical different people, and are the one case where the
merge thresholds have no margin to spare. Siblings and parent-child pairs are
where a wrong merge, when it happens, comes from.

**Video is sampled, not watched.** Five keyframes per clip means someone who
appears briefly may not be found at all.
