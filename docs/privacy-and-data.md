# Privacy and data

Archive is local-first: it does not upload media, catalogue records, diagnostics,
or telemetry. It never moves, renames, edits, or deletes originals. It stores a
SQLite catalogue and derived cache in its per-user app-data directory.

Face processing, pet recognition and search by description all run locally, on
models that execute on this machine. There is no API key to set anywhere in the
app, and no configuration that would send archive content to a service.

**Reading the text inside your documents needs no model and no download.** The
Documents feature parses files that already carry their own text — a PDF's text
layer, a Word document's body, a spreadsheet's cells — and parsing is not
recognition: the readers are the Python standard library and the PDF library
already inside the app. The text it finds is stored in the same per-archive
SQLite catalogue as everything else, and searching it is a SQLite query. No part
of a document, and no phrase you search for, is sent anywhere or written outside
that catalogue.

**Searching documents by meaning does need a model, and it still runs here.**
It downloads once (~129 MB) and then works offline like every other model in
Trove. What it produces is a list of numbers per passage, stored in the same
per-archive catalogue; the passage itself never leaves the machine, and neither
does the question you type.

**Reading the writing in pictures downloads nothing at all.** Text in images is
the one feature whose model weights ship inside the application, so it starts
work immediately with no connection and never fetches anything. It opens your
original files rather than the thumbnails — writing is unreadable at thumbnail
size — and what it produces goes into the same per-archive catalogue as
everything else.

**The only time Archive uses the network on its own** is downloading model
weights: ~550 MB for People and Pets detection on their first run, ~690 MB for
the search-by-description model on an archive's first indexing pass, and ~129 MB
for the search-documents-by-meaning model, all fetched from GitHub or Hugging
Face. Only those downloads happen — no photo,
thumbnail, filename, search query, or catalogue record is sent anywhere. Once
the weights are cached, everything works fully offline. The pet
re-identification and face-embedding models are bundled with the installer and
are never downloaded.

**A feature you did not choose downloads nothing at all.** Each archive is set
up with the features it should run (see the setup screen when adding a folder),
and a feature that is off has no pipeline stage — and a stage is what fetches
weights. An archive set up for indexing and duplicates only never touches the
network. The weights are shared between archives, so the figures above are paid
at most once per machine, not once per folder.

The GUI map's optional "Street map" layer fetches map tiles from a public tile
server, which reveals photo *coordinates* to that server. It is a toggle and can
be turned off for a fully offline plot.
