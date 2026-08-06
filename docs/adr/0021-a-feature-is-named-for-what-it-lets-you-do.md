# 0021. A feature is named for what it lets you do, never for what it reads

- **Status:** Accepted
- **Date:** 2026-08-05

## Context

`features.py` already enforced that a feature is called *one* thing across the
four surfaces that name it — the setup card, the Overview card, the sidebar chip,
and the Browse result heading. `card_label` exists for exactly that, and the
docstring there records the drift it closed: an archive configured with "Search
by description" that then reported progress as "Semantic indexing".

That solved consistency per feature and said nothing about consistency *across*
the table. By the time there were eight features, the labels were written to
three different rules at once:

| Label | What the name named |
| --- | --- |
| Indexing | the process |
| Duplicates, People, Pets, Places | the collection you get |
| Search by description | what you can do |
| Documents, Pictures of text | **the files they read** |

The last row is the one that caused damage, and not only as untidiness.
**"Documents" named an input, and named it wrongly.** A scanned contract is a
document by any ordinary reading, and a scan is precisely what that half cannot
read — so its own `detail` string had to spend a sentence apologising for the
name above it ("A PDF that is only pictures of a page holds nothing for this to
read — that one is Pictures of text"). A name needing a disclaimer is doing
negative work: the reader has to be told what it does *not* mean.

The internal prose showed the same thing from the other side. Nothing in the code
could settle on what to call these two, so comments, docstrings and ADRs
accumulated *four* wordings for one pair — "Documents", "Pictures of text",
"Text in images", "text in pictures" — including inside `services/documents.py`,
the module that implements them.

The tempting fix was to name every feature after its mechanism: People
clustering, Semantic indexing, Text extraction. Rejected, for two reasons. The
mechanism vocabulary is *already in the table* as `verb` + `noun`, which is what
an Overview card says while a stage runs ("Indexing photos for search…"), so it
is present on the surface where mechanism belongs rather than missing. And four
of the eight labels double as the name of the nav section they unlock
(`sections=("people",)`), so "People clustering" on the switch and "People" on
the page it opens would re-create the drift this module exists to prevent.

## Decision

**A feature's label answers "what does this let me do", in the words of someone
who has not read the code.** Two shapes follow from that, because there are two
kinds of feature:

1. A feature that **unlocks a section** is named after that section: Indexing,
   Duplicates, People, Pets, Places. The switch and the page it opens are one
   word, and cannot drift.
2. A feature that **widens the search box** and unlocks nothing is named
   `Search by ‹what you type against›`: Search by description, Search by
   document text, Search by picture text — beside the way no feature owns,
   Search by filename (`_NAME_WAY`).

So `documents` and `ocr` are **Search by document text** and **Search by picture
text**. The mechanism words stay where they were already working: `verb`/`noun`
for the running line, and the ADRs for the reasoning.

**A shared card's fused wording is stated, not derived.** `_joined` composes
"People & pets" from two labels, and cannot compose these two: they share both a
prefix and a noun, so joining gives "Search by document text & search by picture
text" — three words of it redundant, and an "&" where what is true is "or", one
box looking in two places. `_FUSED_LABELS` holds the one string that cannot be
built, `Search by document or picture text`, next to the labels it stands for.

## Consequences

- The apology is gone from `documents.detail`. "A scanned PDF stores no such
  text, however much writing is on the page; that one is Search by picture text"
  is a signpost rather than a correction, because the two names now differ by
  the thing that actually distinguishes them — where the text is, not what the
  file is called.
- **`_FUSED_LABELS` is the one place a name can drift from the labels it stands
  for**, since it is a third string rather than a function of the other two.
  `tests/unit/test_features.py` holds it shut: the fused wording has to contain
  the word each half contributes, so renaming a half fails a test.
- A feature page's `title:` frontmatter is a fifth surface, and it was already
  hand-written — `docs.py` serves `feature_label` from the catalogue for the
  heading but takes `title` verbatim for the rail. Both text pages carried the
  old name. They now match, and a test asserts it rather than trusting the next
  rename to remember.
- The rule is only load-bearing for *new* features, which is where it earns its
  place: a capability arriving as a row in this table now has a naming question
  with an answer, instead of being named by whoever wrote the row.
- Not a user-visible behaviour change of any kind, and nothing is re-read: these
  two features had not shipped under the old names, so the rename lands in the
  same release that introduces them.
