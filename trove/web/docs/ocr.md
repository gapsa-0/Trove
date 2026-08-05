---
title: Pictures of text
summary: Reading the writing in screenshots, photos and scanned PDFs.
feature: ocr
---

{{tagline}}. A photographed receipt, a screenshot of a confirmation, a contract
your scanner turned into a picture of a page — all of them hold words that
nothing else in Trove can see, because as far as the file is concerned they are
not text at all. They are pixels arranged to look like text.

This is what **Documents** cannot do, and the two work together: Documents reads
the files that carry their own text, this one reads the files that only look
like they do.

## What it reads

Photos, screenshots and scans in the usual picture formats, and — the case it
really exists for — **PDFs that are scans rather than documents**.

A PDF is decided page by page. A forty-page contract with a scanned appendix
gets both treatments: the pages with a text layer are read from it, the pages
that are pictures of paper are read as pictures, and the result is one document
in page order. A page is only read as a picture when it has almost no text *and*
is mostly covered by a single image — a title page or a divider has hardly any
text either, and there is nothing in it for this to find.

It reads Spanish and English, accents included. No language needs choosing.

## What it costs, honestly

**This is the slow one.** Every picture has to be opened and looked at, and
unlike everything else in Trove it cannot use the small thumbnails the app
already has — writing disappears at that size, so the original file is opened
each time.

Roughly half a second per picture on a typical machine. That means:

| Pictures in the archive | Roughly |
| --- | --- |
| 5,000 | under an hour |
| 20,000 | 3 hours |
| 100,000 | overnight |

It runs alongside everything else and never blocks browsing, and it stops and
resumes safely — closing Trove mid-run loses at most the picture being read at
that moment. But it is the one feature worth deciding about rather than
switching on by reflex.

Two things bound it: a picture larger than 64 MB is skipped, and so is a PDF of
more than 200 pages, both with that reason recorded.

## What it costs to install

Nothing to download. This is the only model in Trove that ships with the
application rather than being fetched on first use, so switching it on starts
work immediately even with no connection.

## How it works

**Finding the writing, then reading it.** Two different jobs, and they run at
different sizes. Finding *where* writing is happens on a shrunken copy of the
picture, because that step runs on every single image and its cost is set by how
large the image is. Reading the writing then happens on the full-resolution
original, so small print is still legible. Doing both at full size would take
nearly three times as long for exactly the same text.

**A confidence score per line.** The reader reports how sure it is, lines it is
unsure of are dropped, and the average is kept with the document. That is what
lets a search result say the text was *read from a picture* rather than quoting
it as though it were typed — because unlike a document's own text, this is a
best guess.

## What it does not do

It does not find writing that a person could not read either: too small, too
blurred, too dark, or at an angle. And it is not a transcription service — the
words come out roughly in reading order, but the layout of a table or a form is
not preserved.

If an archive is mostly photographs of the world rather than of paper, this will
spend a long time confirming that most of them contain no writing. That is
working correctly, and it may still not be worth it.
