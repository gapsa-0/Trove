---
title: Search by description
summary: Finding a photo by describing it, without anything having been tagged.
feature: semantic
---

{{tagline}}. Type "a dog on the beach", "birthday cake" or "someone holding a
newspaper", and the shot comes back without anyone having named or tagged
anything.

This is the most expensive feature to enable ({{download_mb}} MB of model
weights) and the one that most obviously looks like it must be talking to a
server. It is not. Both halves of the model run here.

## How it works

**One vector per photo.** During indexing, every photo and video goes through
the vision half of SigLIP 2 and comes out as 768 numbers: a point in a space
where "what the picture shows" is a direction. Photos are read through the same
1024-pixel thumbnail the app already renders, so HEIC and RAW decoding, and any
rotation correction, happen once and search sees the photo the way up you do. A
video is sampled at a handful of frames spread across the clip, pulled in from
the very ends where a title card or a black frame tends to sit; those frames
are embedded individually and averaged into one vector, which keeps a clip
comparable to a photo.

**One vector per query.** When you type a description, the text half of the
same model turns it into a vector in the same space. Ranking is cosine
similarity between the two. Nothing is matched on filenames, folders, or tags.

The two towers are loaded independently and lazily: indexing never pays for the
text tower, a search never pays for the vision tower, and the text weights are
not even downloaded until the first search. A first run therefore blocks on 372
MB rather than on all {{download_mb}} MB.

**Correcting the modality gap.** Image vectors and text vectors do not mingle:
they occupy two clusters separated by a nearly constant offset, so raw
similarities are squeezed into a narrow band and every image looks slightly
closer to every other image than to the words describing it. Subtracting each
side's own mean before comparing collapses that offset and spreads the scores
back out.

The effect is not cosmetic. Measured across 41 subjects on one archive, raw
scores spanned 0.046 to 0.146 and the ranking was actually inverted, with a
present subject scoring *below* an absent one:

```scale
range 0 0.45
band 0.046 0.146 muted Raw scores: every subject, present or absent, in one band
band 0.14 0.42 Centred scores: the same 41 subjects, spread out
mark 0.18 semantic_search_min_similarity
note Cosine similarity on one 497-file archive. Raw, "a dog" (present) scored 0.0916 while "the surface of mars" (absent) scored 0.0948, so the absent subject wins and no threshold in that band can separate them. Centred, the same pair becomes 0.3047 against 0.2216, and the floor has somewhere to sit.
```

**Two cuts, not one.** Because the scores shift with the query, a single
threshold cannot work. There are two, and each binds on a different situation:

- An **absolute floor** of 0.18, which only bites when a query's *best* score is
  low, meaning the archive holds nothing like what you asked for. It is
  what makes an unanswerable query return nothing instead of returning junk.
  It gets safer as an archive grows, because the median best score rises with
  file count.
- A **relative floor** of 0.65, which keeps results within that fraction of the
  query's own best score, so the bar travels with the query rather than
  assuming a fixed scale. It is deliberately loose: on a hand-labelled subject
  every result stayed correct well past the cut, so tightening it would only
  discard true matches.

Together they return a median of 10 results for a subject the archive has and 1
for a subject it does not.

**The query is translated to English first.** SigLIP 2 is multilingual but was
trained roughly 90% on English, and the absolute floor is a test on score
*magnitude*, which is language-dependent. Bare Spanish nouns average about
0.18 where English ones average 0.30 ("bosque" 0.130 against "forest" 0.348).
With the translator removed, that floor silenced "bosque", "montaña", "nieve"
and "calle" outright. Worse, the Spanish score populations overlap, so no
threshold works for untranslated Spanish at all. A 26 MB Spanish-to-English
translator therefore runs in the browser, before the query reaches the model.

## The numbers

| Setting | Default | What it does |
| --- | --- | --- |
| `semantic_embedding_model` | `siglip2-base-patch16-256` | Which model produced the stored vectors: provenance, not a knob |
| `semantic_embedding_dimensions` | 768 | Length of each vector |
| `semantic_search_center_embeddings` | true | Subtract each modality's mean before comparing |
| `semantic_search_min_similarity` | 0.18 | Absolute floor, which silences a query the archive cannot answer |
| `semantic_search_relative_floor` | 0.65 | Keep results within this fraction of the query's own best score |

The first two are recorded on every stored vector rather than tuned. The last
two are calibrated *for* centring being on; with it off they would need to be
0.07 and 0.75, and leaving them as they are would put the bar far too high and
make search look broken.

## What runs on your machine

| Model | Role | Size |
| --- | --- | --- |
| SigLIP 2 base/16 @256, vision tower | One vector per photo, during indexing | 372 MB |
| SigLIP 2 base/16 @256, text tower | One vector per typed query | the rest of {{download_mb}} MB |
| Bergamot, Spanish→English | Translating the query before it reaches the model | 26 MB |

The SigLIP weights are the official Apache-2.0 ONNX exports. Everything runs
through onnxruntime on the CPU. This feature used to call a cloud API, and was
the one place the app sent photos and typed queries off the machine; that path
is gone, not merely unused.

## What it gets wrong

**It describes, it does not read.** Text in a photo is not searchable. "The
receipt from the hotel" will not find a receipt by what is printed on it.

**It has no idea who anyone is.** "Photos of my mother" is meaningless here,
since that is what [People](people.md) is for. Descriptions work on what a stranger
could see in the frame.

**Counting and spatial relations are weak.** "Three people" and "the dog on the
left" are not reliably understood. Nouns and scenes work far better than
relationships between them.

**Languages other than English and Spanish.** Only Spanish is translated. A
query in another language reaches the model directly, where the magnitude
problem described above applies to it in full and the absolute floor may
silence it entirely.

**An unanswerable query returns almost nothing, on purpose.** The absolute
floor is what produces that, and it is the intended behaviour rather than a
failure to find anything. A page of unrelated photos is a worse answer than an
empty one.
