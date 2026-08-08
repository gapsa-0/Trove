# 0003. All machine learning runs locally, on onnxruntime

- **Status:** Accepted
- **Date:** 2026-08-01

## Context

This is a family-photo archive tool. Sending any of that content to a cloud
API — for embeddings, face detection, or anything else — is a hard product
constraint, not a preference to be traded off against convenience or model
quality. A previous version of the semantic-search feature did call a cloud
multimodal embedding API (Voyage) with a user-supplied key; that dependency
is gone, and this ADR records both the current state and the removal.

## Decision

Every model the app runs — search by description, face detection and
embedding, pet detection and re-identification — runs locally through
`onnxruntime` (plus OpenCV's `cv2.dnn` for the YOLOX pet/animal detector).
Nothing is sent to a cloud vision or embedding API. Specifically:

- Search by description embeds photos and queries with **SigLIP 2**
  (`google/siglip2-base-patch16-256`, Apache-2.0) locally —
  `trove/embeddings/backend.py`. Its module docstring states
  plainly: "This replaces the Voyage multimodal API — the one place the app
  used to send [content] out."
- Faces are detected with **SCRFD** (`det_10g`, from the InsightFace
  `buffalo_l` pack) and embedded with **AdaFace ir101/WebFace12M** — both
  documented in `trove/config/settings.py`'s comment block above
  `faces_det_size`, and both run through `onnxruntime` from
  `trove/faces/backend.py`, not through a network call.
- Pets are detected with **YOLOX** and re-identified with a **DINOv2**
  checkpoint exported to ONNX — `trove/pets/backend.py` and
  `tools/build/dinov2_pet_export.py`.

**Torch and transformers are explicitly excluded from the packaged desktop
build**, and this exclusion is a direct consequence of running everything on
onnxruntime rather than on those libraries. `packaging/trove.spec`
carries an `excludes` list (`"torch", "torchvision", "torchaudio",
"transformers", "sentence_transformers", ...`) with a comment explaining why:
the app itself never imports either package, but scikit-learn and SciPy reach
for torch through their `array_api_compat` shims, so on a developer machine
that happens to have torch installed (anyone who has run
`tools/build/dinov2_pet_export.py`, which does need torch + transformers to
export the DINOv2 checkpoint in the first place), PyInstaller would otherwise
silently bundle roughly 700 MB of it into every build. `docs/release.md`
documents the same exclusion and gives the same reasoning, and warns that the
list must stay narrow: `onnx` and `skimage` "look dev-only but are NOT" —
`insightface` imports both at runtime, and excluding them would silently
disable face detection in the packaged app.

**The retired cloud path.** A previous version of semantic search called
Voyage's multimodal API with a user-supplied key. That path is retired:
`trove/config/settings.py` keeps `_SUPERSEDED_SECRETS =
("voyage_api_key",)` and a `discard_superseded_secrets()` function whose
purpose is to remove that credential from `secrets.json` (and delete the file
entirely if nothing else is left in it) on every start, with the comment "a
live credential left readable on disk for a feature that no longer exists is
a privacy wart, not merely dead data." `Config.load()` similarly discards any
persisted `semantic_embedding_model`/`semantic_embedding_dimensions`/
threshold values if they still name a `voyage`-prefixed model, so an existing
install's `config.json` cannot keep the old scale of numbers alive
indefinitely. A grep across `trove/` for `voyage`/`anthropic`/
`openai`/`cloud` turns up only comments and identifiers documenting this
retirement (`_SUPERSEDED_SECRETS`, docstrings in `embeddings/backend.py`,
`services/semantic.py`, `pipeline/runners/semantic.py`, and a note in
`services/_common.py` that an older run's rows are still on disk and must be
treated as stale, not falsely marked current) — no code path constructs a
request to a cloud endpoint, and no credential for one is read anywhere
outside the cleanup logic that deletes it.

## Consequences

- First run needs network access exactly once per model, to download its
  weights (see `docs/release.md`): the OpenCV Zoo YOLOX detector (~35 MB) and
  the InsightFace `buffalo_l` pack (~184 MB) are fetched from a stable
  upstream URL; SigLIP 2 downloads itself on first archive-indexing run,
  about 690 MB (README, "Privacy"); the DINOv2 pet re-identification model
  is the one exception with no upstream URL, so it ships inside the packaged
  build instead (~85 MB) rather than being fetched. After those downloads,
  the app is fully offline for all media processing.

  > **Amended 2026-08-08.** Nothing ships inside the packaged build any more.
  > The two weights with no upstream — DINOv2-pet and AdaFace — are re-published
  > as release assets on this repository and fetched like the rest, which is what
  > took 349 MB out of every installer; ADR 0019's amendment did the same for the
  > PP-OCR weights and the query translator. The claim above about *where* they
  > come from is stale; the claim this ADR is actually about — that inference is
  > local, and the network is touched once per model and never again — is not.
- The one deliberate exception to "no network calls" anywhere in the app is
  the GUI map's optional street-map tile layer, which sends photo
  *coordinates* to a public tile server — never the photos — and can be
  turned off for a fully offline plot (README, "Privacy").
- Because nothing depends on torch or transformers at runtime, the packaged
  build stays the same size regardless of what happens to be installed on
  the machine that builds it.
