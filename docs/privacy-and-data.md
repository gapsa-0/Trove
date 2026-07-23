# Privacy and data

Archive is local-first: it does not upload media, catalogue records, diagnostics,
or telemetry. It never moves, renames, edits, or deletes originals. It stores a
SQLite catalogue and derived cache in its per-user app-data directory.

Face processing runs locally. Semantic search is optional and off by default; do
not enable any configuration that sends archive content to a service unless its
privacy implications have been reviewed.
