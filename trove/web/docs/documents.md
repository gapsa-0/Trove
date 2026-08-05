---
title: Documents
summary: Reading the words inside PDFs and office files, so a phrase finds the file.
feature: documents
---

{{tagline}}. Trove already knew your documents were there — it listed them with
a date and a folder like everything else — but what they *said* was invisible.
This reads them, so "arrendamiento" finds the contract without you remembering
that you saved it as `scan_0043.pdf`.

Nothing is downloaded for this. There is no model: reading a document is
parsing, not recognition, and every reader here is either the Python standard
library or one small PDF library that ships with the app.

## What it reads

| Format | Read from |
| --- | --- |
| PDF | its text layer — the characters the file actually stores |
| Word, Excel, PowerPoint | the document body, headers and footers, cells, and each slide |
| OpenDocument (`.odt`, `.ods`, `.odp`) | the document body |
| Text, Markdown, CSV | the file itself |
| Web pages (`.html`, `.mhtml`) | what a browser would show, never scripts or styles |
| Notebooks (`.ipynb`) | the cells you wrote, never their outputs |

Spreadsheet numbers are kept, not just the words around them — an invoice total
is exactly the sort of thing you go looking for in a folder of paperwork.

## The two things it cannot do

**A scan is not a document.** A PDF produced by a scanner or a phone camera has
no text layer at all: it is a picture of a page, and there is nothing in the
file for a reader to read. Those show as skipped, with that reason, and they are
what **Text in images** is for. If your archive is mostly scanned paperwork,
this feature alone will find very little in it — that is not a fault, it is the
wrong half. Switching the other one on later re-reads every file this one
had to pass over.

**Older Office files cannot be read.** `.doc`, `.xls` and `.ppt` — the formats
from before 2007 — are a completely different kind of file inside, and there is
no reliable way to read them without shipping a great deal of software that
exists for nothing else. They are listed and dated like any other file, and
reported as an unsupported format rather than half-read into something
misleading.

## How it works

**Only one copy of anything.** The stage runs after duplicates have been
grouped, so it reads the copy Trove shows and skips the ones it hides. A
document you have three times is read once.

**Passages, not whole files.** A document is cut into overlapping passages of
about 1,200 characters, split at paragraph and sentence boundaries so a passage
begins and ends on whole words. That is what lets a result show you the
sentence that matched, with the page it was on, rather than handing you a
forty-page PDF and leaving you to search it again yourself. The overlap is
there so a sentence spanning a boundary is still findable.

**Accents do not have to match.** Searching for `peticion` finds `petición`.

## What it costs

Very little. Reading a document is bounded by how fast the disk hands it over,
so a few thousand files is minutes rather than hours, and it runs alongside
everything else in the pipeline. One file cannot hold up the rest: anything over
64 MB is skipped with that reason rather than parsed.

Interrupting is safe. Progress is written per file, so closing Trove mid-run
loses at most the document being read at that moment, and reopening carries on
from there rather than starting again.

## When it re-reads a file

Only when something has actually changed:

- the file's contents changed on disk;
- Trove's readers changed in a way that would produce different text;
- you switched **Text in images** on or off, which changes what can be got out
  of the files this one skipped.

Switching Documents off does not delete anything. The text already read stays in
the catalogue, and switching it back on resumes rather than restarts.
