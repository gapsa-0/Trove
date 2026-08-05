# 0020. Browse groups results by ranking, not by reader

- **Status:** Accepted
- **Date:** 2026-08-05

## Context

Five separate things can put a file in front of a typed query. Indexing records
every file's **name**. Search by description embeds every **photo**. Documents
reads a file's **text layer**; Text in images reads **writing off pixels**; and
Search by meaning turns the passages either of those produced into vectors.

Browse's copy had not kept up with any of it. It described itself in one of four
hand-written sentences ("Look through every item, by filter or by description"),
labelled results only when two groups happened to collide, and never said how a
given result had been found. Three of the five ways were invisible on the screen
that ran them.

The obvious fix is to make the five ways visible: a row per way in a panel that
says what can be searched, and a group of results per way underneath. That is
wrong, and it is wrong in a way that only shows up once you try to build it.

## Decision

**The screen names five readers and draws three groups.** A reader is a thing
that fills an index; a ranking is a thing that answers a query. They are not the
same list, and the screen says so:

| Group | Endpoint | Fed by |
| --- | --- | --- |
| File names | `/api/media?name=` | the name Indexing recorded |
| What your files say | `/api/browse/text/search` | Documents, Text in images, Search by meaning |
| What your photos show | `/api/browse/semantic/search` | Search by description |

`RANKINGS` in `trove/web/static/js/library.js` is that table. Which readers are
live for a group is derived from the archive's features, and it is what the
group's sentence is composed from — an archive that reads only pictures is never
promised its documents.

### Why the text group cannot be split

Documents and Text in images write into the *same* `doc_chunks` and the same
FTS5 index; a hit's reader is a property of the file's `doc_text` row, not of a
separate search. And Search by meaning is not a third list either: it is fused
into the word ranking by reciprocal rank inside one request
(`services/text_search.py:_rrf`), because **a document both halves found
outranks one only a single half did**. That property is the entire justification
for fusing at all (ADR 0018), and drawing the halves as separate groups would
throw it away — the user would see two lists, each missing the agreement that
made the top result the top result.

So the readers are reported **per result** instead. `text_search` returns
`reader` (`documents` / `ocr`, from the extractor on the row) and `found_by`
(`words` / `meaning` / `both`), and Browse draws a badge only where the answer
varies inside a group: the reader badge only when both readers are on, and the
meaning badge only on a hit the vectors alone found. In the name and photo
groups every result arrived the same way, so a badge there would repeat the
heading on every tile and spend the caption — which is the file's name — to say
nothing.

### Why file names are always searched

They used to be the *fallback*: `gridRequest` sent the query to the description
index **or** to the name filter, never both. So switching Search by description
on silently removed the ability to find a file by its name — `IMG_2019` was
embedded as a picture description, scored below the absolute relevance floor
(ADR unnumbered; see `web/docs/search.md`), and returned nothing at all. The two
are now separate groups that both run, which costs one extra scan per search.
That is a cost the name path already accepted for itself (`services/browse.py`:
`instr` over a computed basename cannot use an index), and it buys back a way of
searching that the most-configured archives had lost.

### Why an empty group still reports

A group that found nothing is hidden and named on one line at the foot
("Nothing found in *what your photos show*"). Both halves of that are the
decision. Saying nothing is what left an archive with one feature unable to tell
whether the others had run; but three empty headings stacked above the results
that did land is worse than the missing label it replaced.

## Consequences

- Adding a reader to an existing index is a change to one `readers` list and the
  sentence composed from it — not a new group, and not a fourth grid.
- Adding a genuinely new *ranking* means a new grid, and `GRID_IDS` plus
  `activeGrids()` is where that cost is paid. The paging, sentinels and
  generation guard are already shared.
- The panel and the result headings are one list rendered in two states, so a
  feature that gains a reader cannot leave a stale promise behind. This is the
  same rule `features.py` states for its own prose, applied to the screen those
  features feed.
- `text_summary` needs the live reader set to mean anything, so it takes
  `extractors` rather than defaulting. Counting `media_type='document'` was only
  ever right while Documents was the only reader.
