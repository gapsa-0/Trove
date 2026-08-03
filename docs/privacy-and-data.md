# Privacy and data

Archive is local-first: it does not upload media, catalogue records, diagnostics,
or telemetry. It never moves, renames, edits, or deletes originals. It stores a
SQLite catalogue and derived cache in its per-user app-data directory.

Face processing, pet recognition and search by description all run locally, on
models that execute on this machine. There is no API key to set anywhere in the
app, and no configuration that would send archive content to a service.

**The only time Archive uses the network on its own** is downloading model
weights: ~550 MB for People and Pets detection on their first run, and ~690 MB
for the search-by-description model on an archive's first indexing pass, both
fetched from GitHub or Hugging Face. Only those downloads happen — no photo,
thumbnail, filename, search query, or catalogue record is sent anywhere. Once
the weights are cached, everything works fully offline. The pet
re-identification and face-embedding models are bundled with the installer and
are never downloaded.

The GUI map's optional "Street map" layer fetches map tiles from a public tile
server, which reveals photo *coordinates* to that server. It is a toggle and can
be turned off for a fully offline plot.
