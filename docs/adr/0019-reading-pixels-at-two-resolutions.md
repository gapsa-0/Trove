# 0019. Reading picture text runs at two resolutions, and arbitrates per page

- **Status:** Accepted
- **Date:** 2026-08-05

## Context

Search by document text (ADR 0017) reads files that carry their own text. It
finds nothing in
a scan, because a scan has no text in it — it is a picture of a page. That is the
archive the feature system was built for, so the remaining half was always going
to be needed.

The plan for it rested on two numbers, and both were wrong.

## Decision

**RapidOCR, driving PaddleOCR's PP-OCRv6 detection and recognition on the
onnxruntime already here.** Detection runs on a downscaled copy of the image;
recognition runs on crops of the original. Which pages of a PDF need reading as
pictures is decided per page, in the same pass that read its text layer.

### The measurement that changed the design

The plan proposed a **cheap detection gate**: run the small detection model
alone on every photograph, and skip recognition entirely when it finds no text
boxes. That was premised on detection being an order of magnitude cheaper than
recognition, and it is not. Measured on a 4-core machine:

| | |
| --- | --- |
| photograph, detection only | 1.47 s |
| photograph, full detection + recognition | 1.38 s |
| text page, full pass | 2.60 s |

**Detection is the cost.** On a photograph with no writing in it — the
overwhelming majority of any archive — recognition never runs anyway, so the
gate skips the small half and saves nothing.

What is true is that detection's cost scales with input size:

```
1600px -> 1.51s      960px -> 0.62s
1200px -> 0.87s      736px -> 0.57s     512px: no faster, and boxes start missing
```

So the design became: **detect small, recognise large.** Detection sees a copy
scaled to 736 px on its longest edge; the boxes it returns are scaled back into
the original's coordinates, and recognition reads crops of the full-resolution
image. Small print stays legible because the recogniser never sees a downscaled
pixel. On a 2480×3508 scan with 34 px body text, the two paths return
**byte-identical text**, and a text-free photograph drops from 1.51 s to 0.59 s.

A test pins that equality, because it is the entire justification for a design
that is otherwise just more complicated than reading everything once.

### The other number that was wrong

Everything published about this feature during planning quoted **8–20 hours** for
a large archive. That figure was 150,000 files × 0.2–0.5 s, where the per-image
estimate was a guess never measured, the file count was inherited from comments
elsewhere in this repository, and the arithmetic assumed the gate that was
subsequently designed in. Three soft numbers multiplied together.

The measured figure is ~0.59 s per picture with the design as built, so 100k
pictures is an overnight run and 5,000 is under an hour. The feature's own copy
now says that, in those terms, rather than quoting a range.

### The arbitration, and the false positive that matters

A PDF page is read as pictures when **both** of these hold:

- its text layer yields fewer than `ocr_text_layer_chars_per_page` characters, and
- one image covers at least `ocr_min_image_cover` of its area.

The second condition is the important one, and it exists because sparse text
alone is not evidence. A title page, a section divider, a page holding one table
or one signature all have almost no extractable characters and nothing OCR could
add. On a long document those pages are common, so treating them as scans would
be most of a wasted run — the cost of a false positive here is a full render and
read per page.

Both conditions, and the title-page case specifically, are tested directly
rather than only through a pass.

### Why the two features share one stage

Recorded in ADR 0017 and confirmed by building it: whether a page needs reading
as pictures cannot be known until its text layer has been tried. The file is
opened once, each page is routed on what that open found, and a contract with a
scanned appendix comes back as one document in page order.

### The weights, and the dependency

RapidOCR **ships its models inside its wheel** — 31.7 MB of ONNX. That makes it
the only model in this app with no runtime download, no new download origin, no
`manifest.json` entry, and no half-installed state: the feature is wholly present
or wholly absent. pip's own hash check covers the weights.

It also removes the per-language weight the plan assumed. PP-OCR's *default*
recognition dictionary is Chinese and English and has no `á é í ó ú ñ`, which is
why the plan specified a separate `latin` model from ModelScope. The bundled
PP-OCRv6 model carries its charset as ONNX metadata: **18,708 characters,
including every Spanish accent**. Read out of the file, then confirmed by reading
a rendered page.

`rapidocr-onnxruntime`, which the plan named, cannot be used at all: its current
release declares `requires-python <3.13`, and 3.13 is this project's floor
(ADR 0007).

The cost is one framework dependency and seven packages, against a project that
rejected three Office libraries to avoid exactly that (ADR 0017). The alternative
was implementing DB post-processing here — contour extraction, polygon
unclipping, CTC decode — which is intricate enough that a wrong unclip ratio
produces quietly worse text with no error anywhere. That is the failure mode this
feature can least afford, and the trade was made deliberately.

## Consequences

- **This is the one stage that cannot use the cached thumbnails.** Every other
  pass that looks at pixels reads the 320 px thumbnail the app already has;
  writing is gone at that size. Opening originals is most of what this costs.
- **A picture with no writing is a skip, not a failure**, and that is the
  common case. The reason says so, the file stops being pending, and nothing
  suggests anything went wrong.
- **Low-confidence lines are dropped** (`MIN_LINE_CONFIDENCE`). The tail of the
  confidence range is where the recogniser invents words out of texture —
  foliage, fabric, wallpaper — and that noise in a photo archive's index would
  make it worthless. The mean of what survives is stored on the row, which is
  what lets a result be shown as *read from a picture* rather than quoted.
- **Raw camera formats are never opened.** They are photographs of the world by
  definition, the most expensive to decode, and the least likely thing anyone
  photographed a document with.
- Two bounds keep one file from holding the stage: 64 MB, and 200 pages. A
  2,000-page scanned book is an hour of OCR on its own.
- `doc_text.wanted` (ADR 0017) is what makes switching this on re-read every
  scan the document half had to pass over. Without it those files carry a current
  hash
  and a current version and would never be looked at again.
