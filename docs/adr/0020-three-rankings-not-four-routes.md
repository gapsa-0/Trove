# 0020. Browse groups results by ranking, not by reader

- **Status:** Accepted
- **Date:** 2026-08-05

## Context

Four separate things can put a file in front of a typed query. Indexing records
every file's **name**. Search by description embeds every **photo**. Documents
reads a file's **text layer**; and Text in images reads **writing off pixels**.

Browse's copy had not kept up with any of it. It described itself in one of four
hand-written sentences ("Look through every item, by filter or by description"),
labelled results only when two groups happened to collide, and never said how a
given result had been found. Two of the four ways were invisible on the screen
that ran them.

The obvious fix is to make the four ways visible: a row per way in a panel that
says what can be searched, and a group of results per way underneath. That is
wrong, and it is wrong in a way that only shows up once you try to build it.

## Decision

**The screen names four readers and draws three groups.** A reader is a thing
that fills an index; a ranking is a thing that answers a query. They are not the
same list, and the screen says so:

| Group | Endpoint | Fed by |
| --- | --- | --- |
| Search by filename | `/api/media?name=` | the name Indexing recorded |
| Documents & text in images | `/api/browse/text/search` | Documents, Text in images |
| Search by description | `/api/browse/semantic/search` | Search by description |

`features.search_ways` is that table, and the labels in it are not new strings:
a way takes the label and mark of the feature that produced it, through the same
`card_label` / `card_icon` the Overview already uses. Browse is the *fourth*
surface to name this work, and it briefly grew a wording of its own — "What your
photos show" for what the setup panel, the Overview card, the sidebar chip and
the documentation all call Search by description. That is precisely the drift
`features.py` documents itself as existing to stop, so the naming lives there
and the screen renders what it is given.

The filename way is the one whose name is free, because no feature produces
it: Indexing records the name, and heading a group of results "Indexing" would
name the stage rather than the answer. Being free, it is phrased the way the
others read -- "Search by filename", beside "Search by description" -- since the
panel lists ways of searching and the headings say how a result was found.

The composition happens server-side (`routes/archives.py:_ways`) and rides on
the archive payload the picker already hands the client, so Browse draws its
headings on the first paint without an extra request. A JS copy of either table
— the labels or the feature-to-page map — is how the four surfaces start
disagreeing again, and the frontend previously kept the second one and had it
missing three features.

### Why the text group cannot be split

Documents and Text in images write into the *same* `doc_chunks` and the same
FTS5 index. There is one ranking over those passages, and a hit's reader is a
property of the file's `doc_text` row rather than of a separate search — so
splitting them into two groups would mean running the one query twice and
partitioning its results by a column, which is two grids drawn over one answer.

So the reader is reported **per result** instead. `text_search` returns `reader`
(`documents` / `ocr`, from the extractor on the row), and Browse draws the badge
only when both readers are on and a hit could therefore be either. In the name
and photo groups every result arrived the same way, so a badge there would
repeat the heading on every tile and spend the caption — which is the file's
name — to say nothing.

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

- Adding a reader to an existing index is a change to `search_ways` and the
  sentence composed from it — not a new group, and not a fourth grid. Its label,
  mark and documentation link all follow from the catalogue entry.
- A feature added without a documentation page fails `test_docs.py` rather than
  quietly losing its link, because `docs.slug_for_feature` derives the mapping
  from the pages' own frontmatter instead of a second table.
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
