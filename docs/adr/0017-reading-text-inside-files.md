# 0017. Reading the text inside files, in one fused stage, with one dependency

- **Status:** Accepted
- **Date:** 2026-08-05

## Context

Trove already catalogued documents — `media/types.py` files 17 extensions as
`document`, and they appeared in the Library grid with a date and a folder — but
what they *said* was invisible. `services/search.py:semantic_pending` even
counted PDFs as pending semantic work, embedded nothing for them, and recorded a
permanent skip. An archive of family paperwork was searchable by filename and
nothing else.

Making the contents searchable is two capabilities, and they cost wildly
different amounts: parsing files that carry their own text (minutes, no model)
and reading text out of pixels (hours, ~13 MB of weights). ADR 0015 built the
feature system for exactly this, and named this work in its closing paragraph.

This ADR records the decisions behind the first of the two, **Search by document
text**, and the ones that shape what comes after it.

## Decision

### One fused `text` stage, owned by both text features

Both features read files, and the same file can need both: a PDF's pages may
carry a text layer, be scans, or be a mix. **Whether a PDF needs reading as
pictures cannot be known until its text layer has been tried**, so the two halves
share one open of the file, as People and Pets share one image decode (ADR 0004).

Splitting them gives one of two bad outcomes. Either OCR opens and parses every
PDF's text layer itself to decide whether to work — the expensive parse twice
over, and `pypdfium2` becomes a hard dependency of the OCR extra as well — or
OCR reads the document reader's *output* to decide, which makes it depend on an
optional stage. That dependency is forbidden (`tests/unit/test_features.py`:
a disabled stage is dropped from the list entirely, so anything waiting on it
waits for a state that can never arrive), and the data-level version of it is
worse than the declared one: on an archive running OCR with the document half off, a
scanned PDF would simply never be read, with nothing anywhere saying why.

`tests/unit/test_features.py` previously hardcoded `assert sd.kind ==
stages.DETECT, "only the fused pass may have two owners"`. That assertion is now
made against `stages.MULTI_OWNER_KINDS`, declared beside the stage table. Fusing
is a claim about the work; a test is the wrong place to keep the list of which
claims have been made.

**The consequence a fused stage cannot avoid**: `doc_text.wanted` records which
halves were switched on when a file was read, and the resumability predicate has
a fourth leg for it. Without that, a scan read once with only the document half
on carries a current hash and a current version, is therefore never pending, and
switching the picture half on afterwards would never bring it back. `pet_scan.
model_source` exists for the same reason on the other fused pass.

### The extraction stack: one dependency, and the four it rules out

**`pypdfium2` for PDFs, and the standard library for everything else.**

The obvious shape of this was four dependencies. It is one, because Word, Excel,
PowerPoint and the three OpenDocument formats are all a ZIP of XML, and getting
the *words* out of them is a walk over text nodes — `zipfile` and `ElementTree`
do it in about 200 lines covering both families. `python-docx`, `openpyxl` and
`python-pptx` exist to model documents: styles, merged cells, formulas, revision
history. None of that is asked for here. Text, Markdown, CSV, HTML and notebooks
are stdlib too.

`pypdfium2` carries a prebuilt PDFium in its wheel (BSD-3-Clause/Apache-2.0), so
no compiler and no system library on the release runners.

Rejected, and why:

- **PyMuPDF** — the strongest extractor available, and AGPL-3.0 or a commercial
  licence. This repository is MIT.
- **`pypdf`** — pure Python and permissive, and it would work. `pypdfium2` wins
  because the same PDFium build also *rasterises*, which is what will let Text in
  images read a scanned page without a second PDF dependency, and renders a first
  page, which is the thumbnail the Library has never had for documents.
- **Docling** (IBM, MIT) — excellent output, pulls torch.
  `packaging/trove.spec` excludes torch deliberately (~700 MB), and the
  `semantic` extra already refuses `transformers` over a 64-token tokenizer.
- **MarkItDown** (Microsoft, MIT) — wraps the same three Office libraries plus a
  large tree, and converts *to markdown* rather than extracting text.
- **tesseract**, for the OCR half — a system binary. The project tolerates
  exactly two (`exiftool`, `ffprobe`) and each one is a packaging problem on
  three platforms. **EasyOCR** and **docTR** are torch. The VLM readers
  (PaddleOCR-VL, dots.ocr, olmOCR) are billion-parameter models wanting a GPU,
  against a CPU-only local-first app. RapidOCR on the onnxruntime already shipped
  for faces, pets and semantic is the answer there.
- **Legacy `.doc` / `.xls` / `.ppt`** — OLE2 compound binaries, not zipped XML,
  with no pure-Python reader worth shipping. Refused by name, per file, so the
  panel's claim about them is checkable where the files actually are. A
  strings-style scrape would produce text that looks like a successful read.

### A passage is sized for a reader, not for a model

A document is cut into overlapping passages of about 1200 characters, on a
sentence or paragraph boundary where one is near.

Nothing downstream requires a particular size — FTS5 indexes a passage of any
length — so the number is chosen for the person reading the result. A chunk is
what a hit *is*: "page 12, this paragraph" is an answer where "this PDF
contains your words somewhere" is a place to start looking, and the snippet and
the page range both come off the same row. Too small and a match loses the
sentence that made it make sense; too large and the page range stops being a
location.

**This is the one number here that a future feature could invalidate**, and it
is worth knowing why before changing it. Any model that embeds these passages
has a token window, and characters do not convert to tokens at a fixed rate:
measured against a real multilingual tokenizer, on chunks this chunker actually
produced, 1193 characters of Spanish prose is 262 tokens while 1195 characters
of CSV rows is 628. Digits, punctuation and delimiters tokenise near-
individually, and a `.csv` is a `document` here. So the ratio has no floor, no
character count can bound tokens, and anything that needs a token bound has to
measure each passage itself and sub-split what does not fit — rather than
handing it to a tokenizer, which truncates in silence and leaves the tail of
exactly the dense numeric passages a paperwork archive is full of unsearchable.

### The full-text index is contentful, and created by a migration

`doc_chunk_fts` is a plain contentful FTS5 table, created in
`db/migrations.py:_migrate_text_index` rather than declared in `schema.sql`.

`executescript` runs `schema.sql` at every job start on every archive, so one
unsupported `CREATE VIRTUAL TABLE` there would fail archives that never asked to
read a document. In a migration, a build without FTS5 simply leaves the index
absent and the feature reports itself unavailable.

Contentful rather than external-content because an external-content index must be
kept in step with `doc_chunks` by delete triggers, and **SQLite fires AFTER
DELETE triggers on a foreign-key cascade only when `PRAGMA recursive_triggers` is
on, which it is not**. `reconcile_root` deletes files wholesale, so the index
would be left addressing content rows that no longer exist — which for an
external-content table makes `snippet()` and `bm25()` return garbage rather than
merely returning too much. Contentful turns that same orphan into a stale rowid
the search's join drops in silence. With no prior FTS5 anywhere in this codebase,
the form that fails safely is worth the second copy of the text it costs.

That leaves exactly one sync obligation, in three functions: `save_chunks`,
`clear_chunks`, and `_delete_root_files`. There are no triggers to reason about.

### Two searches, two groups, never merged

BM25 and SigLIP cosine are different scales measuring different things — which
file *says* this, and which photo *looks like* this — and the semantic cuts in
`config/settings.py` are calibrated against a distribution a merge would quietly
break. Browse shows two labelled groups, served by two endpoints, and text goes
first when it has hits: an exact word match is explainable in a way a cosine is
not.

Two endpoints also means each degrades alone. An install without numpy loses
description search and keeps text search, which needs nothing but SQLite.

**And the obvious way to add meaning to the text half does not work**, which is
worth stating here because everything about it type-checks, runs, and returns
results. Embedding a document's passages with the embedder already in the
repository looks like one line of work: same model, same table, one search
covering both. Two reasons it is not. SigLIP's text tower is configured in the
checkpoint with `MAX_TOKENS = 64`, for captions — a passage of a contract is
300–500 tokens, and the tokenizer discards the rest without raising. And it is
not a text encoder at all: it is one half of a contrastive image–text pair,
trained so a caption lands near *the photograph it describes*, with no training
signal anywhere in it for "these two paragraphs mean similar things". Asked to
place a page of prose it answers with roughly where a photo of a page of prose
would sit, so a tax letter, a lease and a warranty all land close together. No
error, no empty result, and a plausible-looking list of documents in roughly
arbitrary order. Meaning search over text needs its own model and its own
vector space, or it needs not to exist.

## Consequences

- **The search box is gated on either feature, not on Search by description.**
  It used to render only for the latter, which would have left an archive that
  reads its paperwork but declined the 689 MB image model with no way to type
  anything at all. With no image index the query does not reach that endpoint,
  the media group steps aside rather than listing the whole library under a
  search it had nothing to do with, the result-scope control is not drawn (a text
  match is a match, with no cut to relax), and the 23 MB translator is not
  fetched — it exists to help a model trained overwhelmingly on English, and the
  text index matches whatever language the documents are in.
- **The two text features are not a `pairs_with` pair.** That field means
  "each is more accurate for the other being on", which is true of People and
  Pets and is what the panel's note about a lonely half says. These two read
  *different files* — one the text layer, one the pixels — and neither improves
  the other. Their labels say which is which instead. A test
  now also refuses a `pairs_with` naming a feature that does not exist, since the
  panel resolves it by id and a dangling one degrades silently to no note at all.
- **A scan is a skip, not an error.** A PDF with no text layer is not a broken
  file; it needs the other reader. Recording it as an error would put a red count
  on the text card for an archive behaving exactly as designed. The status
  decides only what the card says — an outcome row is written either way, which
  is what stops an unreadable file being re-derived on every pass forever.
- **The FTS5 query is sanitised, not escaped.** `MATCH` takes a query *language*,
  and almost everything a person might type is a hard error in it rather than a
  bad result: `foo"bar` is an unterminated string, `a:b` and `-x` are missing
  columns, `NOT x` is an operator, `*` is an unknown special query. Word-ish runs
  are extracted and everything else is dropped. Each token gets a prefix star, so
  "factur" finds "factura" — without it FTS5 matches whole tokens and every
  plural silently misses.
- **The snippet crosses the wire as control characters, not markup.** FTS5 does
  not escape the document text it returns around a match, so a `<mark>` from the
  server would let a document containing the word `<script>` put it into the
  page. The client escapes the text and substitutes the marks afterwards.
- `stages.py` and `db/database.py` were both split to make room
  (`pipeline/cards.py`, `db/migrations.py`), and `Runner.takes_write_lock` is now
  bound to `stages.LOCK_KINDS` by a test — they had always been the same fact
  stated twice in two files, with nothing checking they agreed.
- The other half of this work gets its own ADR: the arbitration rules for Text
  in images (ADR 0019).
