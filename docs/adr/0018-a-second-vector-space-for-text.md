# 0018. Document meaning is a second vector space, not more rows in the first

- **Status:** Accepted
- **Date:** 2026-08-05

## Context

Trove already had an embedder and a table of vectors. Search by description
embeds photos and videos with SigLIP 2 and ranks them by cosine against a typed
query, and `semantic_embeddings` holds the result. Once Documents was reading
text out of files (ADR 0017), the obvious next step looked like one line of
work: embed the text with the same model, put it in the same table, and let one
search cover both.

It does not work, and the reason is not obvious from the outside — which is
exactly why it is written down here. Everything about that plan type-checks,
runs, produces vectors of the right width, and returns results.

## Decision

**Document meaning gets its own model, its own table and its own ranking.**
`trove/embeddings/text_backend.py` holds `intfloat/multilingual-e5-small`,
`doc_chunk_embeddings` holds its output, and a vector from it is never scored
against a vector from `semantic_embeddings`.

### Why SigLIP cannot do this

Two reasons, either of which is sufficient.

**`MAX_TOKENS = 64`.** SigLIP's text tower is configured, in the checkpoint, for
captions. A passage of a contract is 300–500 tokens. What arrives at the model
is the first sentence and a half, and the tokenizer discards the rest without
raising.

**It is not a text encoder.** It is one half of a contrastive image–text pair,
trained so that a caption lands near the *photograph it describes*. There is no
training signal anywhere in it for "these two paragraphs mean similar things".
Asked to place a page of prose, it answers with roughly where a photo of a page
of prose would sit — so a tax letter, a lease and a warranty all land close
together, and close to a photograph of any document. The vectors are perfectly
well-formed. The space simply does not encode the distinction being searched on.

The failure mode this produces is the worst shape available: no error, no empty
result, and a plausible-looking list of documents in roughly arbitrary order.

### Why multilingual-e5-small

- **MIT**, which matters for a repository that ships an installer and a
  `THIRD_PARTY_NOTICES.md`. EmbeddingGemma-300M scores better per parameter and
  is governed by the Gemma Terms of Use — not OSI-approved, with a prohibited-use
  policy that flows down to every downstream user and a unilateral termination
  right. That is a real cost for a quality gain nobody would notice on household
  paperwork, at twice the download.
- **No new Python dependency.** `onnxruntime`, `numpy` and `tokenizers` are
  already the `semantic` extra, and e5's XLM-R SentencePiece vocabulary loads
  through the same `tokenizers`.
- **An official ONNX export in the model repository**, so there is no
  `tools/build/*_export.py` to write and maintain, unlike AdaFace and DINOv2.
- **Genuinely multilingual and symmetric**, which retires the translator for this
  half of search: a Spanish query finds Spanish text directly. Search by
  description translates a Spanish query first, because SigLIP was trained
  overwhelmingly on English and reads text *inside* pictures; none of that
  applies here.
- 384 dimensions against SigLIP's 768, so a passage vector is 1.5 KB.

Rejected: **jina-embeddings-v5-text-small** and **Qwen3-Embedding-0.6B**, both
the strongest in their class and both ~677M parameters — several hundred MB
quantised, against 118 MB, for an archive of household documents. **Static
embeddings** (model2vec/potion) are ~30 MB and essentially free to run, but are
bag-of-embeddings with no context, and on exactly the paraphrase queries that
justify having this feature they lose most of their advantage over FTS5.

### The int8 export, checked rather than assumed

The shipped weights are `model_qint8_avx512_vnni.onnx`. That is a U8S8
quantisation, which on x86 **without** AVX512-VNNI can saturate and lose
accuracy — a documented onnxruntime caveat, and the development machine is AVX2
only. Measured against the 470 MB fp32 weights over the same passages: identical
ranking, largest similarity difference 0.0044, per-vector cosine never below
0.9949. The concern does not materialise; the 352 MB saving does.

### Two rankings, fused by rank

BM25 and a cosine are not comparable and no normalisation of them is meaningful,
so they are combined by Reciprocal Rank Fusion (k=60) — positions, not scores.
The consequences of that choice are recorded in the code and are load-bearing:
collapse each list to one row per file *before* fusing, cut *before* fusing
(an RRF score is in reciprocal-rank units and a floor on it means nothing), and
break ties deterministically or a page boundary moves between identical
requests.

This is deliberately *not* what happens between text search and description
search. Those two stay in separate labelled groups (ADR 0017): they answer
different questions about different media, and fusing them would be merging two
answers rather than two views of one.

## Consequences

- **Two embedders live in `trove/embeddings/`**, and the module docstring of each
  opens by saying why the other exists. That is the whole defence against
  someone later noticing the duplication and unifying them.
- **The chunker cannot bound the token window, so the embedder must.** Measured
  against this tokenizer, a 1195-character passage of CSV rows is 628 tokens
  where the same length of prose is 262 — the ratio has no floor, so no
  character target fixes it (ADR 0017). `embed_passages` therefore splits an
  over-long passage into windows, embeds each, and averages them back into one
  vector so `doc_chunk_embeddings.chunk_id` stays a primary key. Handing it to
  the tokenizer instead truncates in silence, and the tail of exactly the dense
  numeric passages a paperwork archive is full of becomes unsearchable.
- **Three things about the recipe fail quietly**, so each is stated where it
  happens and pinned by a behavioural test: the asymmetric `query: ` / `passage: `
  prefixes, masked mean pooling (e5 has no pooler output — do not reach for
  SigLIP's pooler index), and reading the session's input names rather than
  hardcoding them.
- **The similarity floor is not comparable to the image one.** e5's cosines sit
  in a narrow, high band — unrelated text scores ~0.75, not ~0.0 — so
  `text_search_min_similarity` and `semantic_search_min_similarity` must never be
  reasoned about by analogy. The default is measured, but from five hand-written
  pairs, and says so.
- The stage depends on `dedup`, not on `text`, because a stage may not depend on
  one an archive can switch off. It stands to the text stage as semantic stands
  to scan, with the same visible cost: the card can read done and then queued
  again ten seconds later while text is still committing.
- Search by meaning is the second feature, after Documents, that unlocks no nav
  section — both are the one search box in Browse, and both are gated where they
  are used rather than by dropping a section.
