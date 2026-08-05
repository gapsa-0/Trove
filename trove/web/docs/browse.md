---
title: Searching from Browse
summary: One box, up to three ways of answering it, and how to tell which one found a result.
---

Browse has one search box, and what you type goes down every route this archive
can take at once. Each route gets its own group of results, headed by the way
that produced them, because they answer different questions and their scores
share no scale.

## The three ways

Each one is named after the feature you switched on to get it, so the heading
over a group of results is the same words as the card on the setup screen, the
card on the Overview and the page documenting it. The only exception is the
first, which is not a feature and which nobody chose -- so its name is free,
and it is phrased the way the others read.

**Search by filename** is always available. Every word you type has to appear
somewhere in the file's own name, in any order, so "beach 2019" finds
`2019_beach_trip.jpg`. This needs no index, no model and no feature: it is the
one way every archive has, whatever else it runs, and the part of the name that
matched is marked on the result.

**Documents** and **Text in images** share one way, headed with whichever of
them you have on — or *Documents & text in images* when both are. Documents
reads PDFs, Word, Excel, PowerPoint, OpenDocument, plain text, CSV, web pages
and notebooks; [Text in images](ocr.md) reads the writing off photos,
screenshots and scans. They write into the same passages, so either one alone
gives you this group, and a result says which of the two found it.
[Search by meaning](meaning.md) joins the same way rather than adding another:
it puts a second ranking over those same passages and fuses it with the first,
so a clause about the rent can match "how much is the rent" without sharing a
word with it — and a result found that way says so.

**Search by description** ranks photos and videos by how close the picture is
to your words, with nothing having been tagged. See
[Search by description](search.md).

## Reading the results

Each group is headed by the way that found it and how many it found. They are
ordered by how explainable they are: a name match is the most literal thing
Browse can show you, a passage with your word in it is next, and a picture that
merely looks like what you described is last.

**Every way links to what documents it.** The panel draws one mark per feature
feeding a way; pressing it opens that feature's page. The shared way has two or
three, which is why they are marks rather than a row of link text.

**A way that found nothing still reports.** It collapses into one line at the
foot — "Nothing found in what your photos show" — rather than a heading over an
empty row. That line is there because "the documents were searched and none
matched" is an answer, and its absence used to leave people wondering whether a
feature had run at all.

**Only the text group carries badges**, and only for the facts that vary inside
it: which reader produced the text, when both are on, and whether a passage
matched what you meant rather than what you typed. Everywhere else the heading
above the results has already said how they were found.

## What the panel says before you type

With the box empty, Browse lists the ways it can answer a query, what each one
matches, and how much of the archive it currently covers — how many files have
been read, how many are still queued. It is the same list as the result
headings, which is deliberate: a way that gains a reader gains a row, so what
the screen promises and what it labels cannot drift apart.

An archive that chose none of the optional features still gets the panel, saying
it has one way and that names are always searched.

## What this does not do

**Filters narrow every group at once.** Year, month, type, person and place
apply to all of them, so a filtered search is still one question asked three
ways.

**Typing a person's name filters by them.** If a word matches somebody you have
named in [People](people.md), it becomes a person filter and stops being
searched for as a word. The box shows it as a chip so the change is visible.

**There is no query syntax.** No quoting, no operators, no field prefixes.
Typing more words narrows every group rather than widening any of them.
