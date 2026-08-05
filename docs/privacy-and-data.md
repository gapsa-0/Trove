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

**The only time Archive uses the network on its own** is downloading model
weights: ~550 MB for People and Pets detection on their first run, and ~690 MB
for the search-by-description model on an archive's first indexing pass, both
fetched from GitHub or Hugging Face. Only those downloads happen — no photo,
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
