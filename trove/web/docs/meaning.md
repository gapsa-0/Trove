---
title: Search documents by meaning
summary: Finding a document by what it is about, rather than by the words it happens to use.
feature: meaning
---

{{tagline}}. Ask "how much is the rent" and the clause saying *importe del
alquiler mensual* comes back, without a single one of those words appearing in
what you typed.

This sits beside the exact-word search that **Documents** already gives you, not
instead of it. The two miss different things, and that is the whole reason for
having both.

## Why both

Exact-word search is unbeatable at what it does. An account number, a surname, a
policy reference — you know the string, and nothing but that string will do. It
also fails completely the moment you cannot remember the wording, which is most
of the time with paperwork you filed years ago.

Search by meaning has the opposite shape. It finds the lease clause when you ask
about rent, and the guarantee when you ask about a broken television. It is also
perfectly capable of returning a document that is *about* the right thing and
does not contain the reference number you actually needed.

So results from both are shown together, blended so that a document found by
both ways ranks above one found by only one. Neither ranking is thrown away.

## It works across languages

The model reads 100 languages and puts them in the same space, so an English
question finds a Spanish document and the other way round. Nothing is translated
on the way — the question and the document are simply understood as meaning the
same thing.

This is a real difference from search by description, which searches your
*photos*: that model was trained overwhelmingly on English, so Trove translates a
Spanish query before using it. Nothing needs translating here.

## What it costs

{{download_mb}} MB, downloaded once the first time an archive uses it, and shared
with every other archive on this machine afterwards. Nothing else is fetched, and
nothing leaves the machine — the model runs here, like every other model in
Trove.

After that it is fast: indexing a few thousand documents is minutes, and it runs
alongside the rest of the pipeline. Reading the documents is the slow part, and
**Documents** has already done it.

## What it needs

**Documents, switched on.** This feature indexes what that one reads, so on its
own it has nothing to work with — the card will simply sit at nothing to do. If
you only have one of the two switched on, it should be Documents.

## When it re-indexes

Only when something changed: a document was re-read because its contents changed
on disk, or Trove's model changed in a way that would produce different results.
Switching the feature off deletes nothing, so switching it back on resumes rather
than starting over.
