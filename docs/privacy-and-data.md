# Privacy and data

Archive is local-first: it does not upload media, catalogue records, diagnostics,
or telemetry. It never moves, renames, edits, or deletes originals. It stores a
SQLite catalogue and derived cache in its per-user app-data directory.

Face processing runs locally. Semantic search is optional and off by default; do
not enable any configuration that sends archive content to a service unless its
privacy implications have been reviewed.

**The one time Archive uses the network on its own** is the first run of People
and Pets detection, which downloads their model weights (~220 MB total) from
GitHub. Only that download happens — no photo, thumbnail, filename, or catalogue
record is sent anywhere. Once the weights are cached, detection, grouping and
recognition work fully offline. The pet re-identification model is bundled with
the installer and is never downloaded.

The GUI map's optional "Street map" layer fetches map tiles from a public tile
server, which reveals photo *coordinates* to that server. It is a toggle and can
be turned off for a fully offline plot.
