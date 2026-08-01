# 0004. People and pets are detected in one decode pass, and arbitrate each other

- **Status:** Accepted
- **Date:** 2026-08-01

## Context

The pipeline used to detect faces and animals as two independent stages, each
decoding every photo on its own — so roughly 150,000 images were decoded
twice for no reason but organisational convenience. Worse than the wasted
decode, the two detectors had no way to correct each other: an animal's own
face could be catalogued as a person, and — the harder case — a person who is
not upright in the frame (lying down, or a photo stored sideways) could be
misread by the animal detector as a dog with real confidence.

This is recorded as one ADR, not two, because it reads like two independent
features ("face detection" and "pet detection") and it is not: the two share
one decode and actively depend on each other's output. Re-splitting this into
two pipeline stages would silently reintroduce both the double decode and the
lost cross-check.

## Decision

`organize_archive/detect/extract.py` is the single fused-detection stage. Its
module docstring states the shape of the decision directly: "The old pipeline
detected pets and faces in two separate stages, each decoding every photo
independently... Here each image is decoded a single time (at
`cfg.detect_max_side`) and both detectors run on that one array."

The pieces:

- **One decode, two detectors.** `_load_bgr` decodes each image once (via
  Pillow, with `draft()` for fast JPEG downscale and EXIF orientation
  applied), and `_detect_on` runs both SCRFD (faces) and YOLOX (animals +
  the COCO `person` class) over that same array.
- **The `person` box arbitrates in both directions.** The cross-check is
  anchored on the COCO `person` box the YOLOX pass already produces
  (`pets/backend.py`). `_drop_human_animals` removes an animal box whose IoU
  with a person box is high — YOLOX's real failure mode on a non-vertical
  human — from the animal results, so a misread person is not catalogued as
  a pet. In the other direction, `_detect_on` drops a face from People only
  when it falls inside an animal box *and not* inside any person box, so an
  animal's own face is kept out of People without also discarding a human
  face that happens to overlap a nearby pet.
- **Faces were rejected as the orientation signal for a good part of the
  work, and the person box is the fallback that survives when a face
  quorum alone is not enough.** `_resolve_rotation`'s docstring lays out the
  ordering: a *quorum* of faces (`orientation_min_faces`, default 3) that
  only resolve at a quarter turn is the strongest evidence — "people do not
  all lie down in the same direction" — but a *lone* rotated face is
  explicitly excluded as evidence: "a lone rotated face is nearly always a
  doll, a cake figurine or someone lying down — that was measured on this
  archive, and single-face evidence is not used at all." When no face quorum
  is available, a YOLOX `person` reading that appears at a quarter turn and
  not upright is the fallback signal, gated much more tightly (an absolute
  score floor, a margin over the upright reading, a frame-share requirement,
  and it must beat the photo's own animal reading) because, per the same
  docstring, "person scores vary far less between turns than face scores
  do" and being wrong is expensive: "turning a correctly stored photo over
  is worse than leaving a sideways one alone."
- **One pipeline stage, one UI card.** The detect stage's own pending query
  (`_pending_where`) treats a file as needing work when it lacks a current
  `face_scan` row *or* a current `pet_scan` row, and processes it as one
  unit — both detectors run and both `faces` and `animal_detections` rows
  are rewritten together, so the cross-check between them is never left
  half-stale. The GUI correspondingly shows one "People & pets" card, not
  two.
- **Videos** are handled by sampling several keyframes per clip
  (`detect_video_frames`, default 5) rather than decoding every frame;
  both detectors run on each sampled frame, and `collapse_video_faces` /
  `collapse_video_animals` then merge detections of the same person or
  animal across frames (by embedding cosine similarity, with species
  additionally required to match for animals) into one row per individual,
  so a person visible across five frames of a video is one result, not five.

## Consequences

- Decoding is the expensive part of this stage; fusing the two detectors
  into one pass halves it for the whole archive.
- The cross-check catches errors neither detector could catch on its own —
  a sideways group photo that would otherwise lose every face from People
  *and* gain a phantom pet is the concrete case the code's own comments cite.
- **Do not re-split this into separate face and pet stages.** Doing so
  would silently reintroduce the double decode this stage exists to avoid,
  and would remove the person-box cross-check in both directions.
