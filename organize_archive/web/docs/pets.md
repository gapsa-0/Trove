---
title: Pets
summary: Finding cats, dogs, birds and horses, and telling one animal from another.
feature: pets
---

{{tagline}}, then works out which of them are the same animal, so a pet gets a
page of its own the way a person does.

Pets shares its work with [People](people.md). Each image is decoded once and
both detectors run on that one array, so enabling both costs barely more than
enabling either. The two also correct each other, which is the most interesting
part of this stage.

## How it works

**Detection.** YOLOX, a general object detector, is run over the decoded image.
Trove keeps the boxes for four classes (cat, dog, bird, horse) above a
confidence of 0.60 and at least 48 pixels across.

**Identity.** Each animal crop is embedded with DINOv2, a self-supervised
vision model, giving a vector that describes what the animal looks like without
having been trained on any specific pet. Two crops of the same animal produce
similar vectors. Grouping is complete-linkage agglomerative clustering at a
cosine similarity of 0.75, and an identity needs at least 2 detections to
become a pet.

Complete linkage here, rather than the average linkage People uses, because
the margin is enormous and there is no reason to spend it:

```scale
range 0 1
band 0 0.30 muted Different animals
band 0.80 0.96 The same animal
mark 0.75 pets_cluster_similarity
note Cosine similarity between DINOv2 embeddings, calibrated on one archive's own photos.
```

**The human veto.** YOLOX has a specific and reproducible failure: a person who
is not vertical in the frame, whether lying down or in a photo stored
sideways, is detected as a dog with real confidence. So the same pass also reports COCO
`person` boxes, at a much lower confidence floor, and an animal box that
overlaps one closely enough is discarded as a misclassified human.

The test is intersection-over-union, not containment, which is what keeps
someone *holding* a cat from vetoing the cat:

```scale
range 0 1
band 0.20 0.63 muted A person holding an animal
band 0.95 0.97 A person misread as an animal
mark 0.80 pets_human_iou
note Overlap between an animal box and a person box, measured on one archive. Below about 0.7 this rule starts discarding pets that are being held.
```

**The animal veto, in the other direction.** A face that sits mostly inside an
animal box is not a human face, and is dropped from People. This is the one
non-human rule in the face pipeline, and it is why cats stop appearing among
your relatives.

**Orientation.** Between them, the two detectors also work out which way up a
photo actually is. Many photos are stored with their pixels turned while their
metadata claims otherwise, which blinds every model downstream. A quarter turn
that yields three confident faces settles it; where the copy is too degraded
for face detection at any angle, a strong `person` reading is the fallback, and
it needs both an absolute score and a clear margin over upright, because
person scores vary far less between turns than face scores do.

A lone subject only decides the photo's orientation if it fills a good part of
the frame. Someone lying on the grass in a landscape shot looks exactly like a
standing person in a sideways one; the difference is that the sideways
portrait's subject covers most of the frame while the person lying down is a
detail of a scene.

## The numbers

| Setting | Default | What it does |
| --- | --- | --- |
| `pets_species` | cat, dog, bird, horse | Classes kept from the detector |
| `pets_min_score` | 0.60 | Minimum detector confidence for an animal box |
| `pets_min_px` | 48 | Minimum box side in pixels |
| `pets_cluster_similarity` | 0.75 | Similarity at which two crops are the same animal |
| `pets_min_detections` | 2 | Detections an identity needs before it becomes a pet |
| `pets_human_min_score` | 0.20 | Confidence floor for the `person` boxes used to veto |
| `pets_human_iou` | 0.80 | Overlap at which an animal box is judged a misread human |
| `pets_face_overlap` | 0.60 | Face-in-animal overlap that marks a face non-human |
| `detect_max_side` | 1280 | Long-side cap for the decode both detectors share |
| `detect_video_same_animal` | 0.80 | Similarity for collapsing one animal across a video's frames |
| `orientation_min_faces` | 3 | Faces that must agree on a quarter turn before it is believed |
| `orientation_person_min` | 0.75 | Absolute `person` score for the orientation fallback |
| `orientation_person_margin` | 0.25 | Margin that fallback needs over upright |
| `orientation_min_subject` | 0.35 | Frame share a lone subject must cover to decide orientation |

## What runs on your machine

| Model | Role | Size |
| --- | --- | --- |
| YOLOX-s (OpenCV Zoo) | Detecting animals, and the `person` boxes used to veto | part of {{download_mb}} MB |
| DINOv2-s, exported for pet re-identification | Identity embedding per animal crop | part of {{download_mb}} MB |

Both run through onnxruntime on the CPU. The DINOv2 export has no upstream to
fetch it from, so packaged builds carry it and a source checkout exports it
once.

## What it gets wrong

**Four species only.** A rabbit, a hamster, a lizard or a fish is not detected
at all. The four classes are the ones the detector is reliable on.

**Same-breed animals of the same colour.** Two black cats, or two dogs of the
same breed, sit far closer together than the 0.30 that separates typical
different animals. This is where a wrong merge comes from, and it is
recoverable by splitting the pet by hand.

**Held pets, at the margin.** The human veto is tuned so that holding an animal
does not discard it, but a photo where the animal and the person occupy almost
exactly the same box, such as a small dog held against the chest and filling
the frame, can still trip it.

**Fur, not face.** DINOv2 describes the whole crop, so a pet that changes
appearance a lot, through a heavy haircut, a coat on and off, or a puppy
growing up, can split into two identities.

**Video is sampled.** Five keyframes per clip, the same as People, so a pet
that wanders through briefly may be missed.
