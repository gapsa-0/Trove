"""Application services: the archive's business rules, one module per domain.

Everything here reads and writes the catalogue on behalf of a caller that has
no database of its own -- the HTTP layer, the CLI, the job runners. Splitting
it by domain (people, pets, places, browse, dedup, search) is what keeps any
one of them small enough to read in a sitting.

Two contracts hold across every module:

* **A function takes a ``db_path`` and opens its own connection.** That looks
  wasteful and is deliberate: handlers run concurrently under
  ThreadingHTTPServer, and a sqlite3 connection may not be shared between
  threads. Connection setup against a local file is cheap; a connection shared
  across threads is a crash.
* **Writes live here, not in the caller.** Despite the name this layer's
  predecessor carried ("queries"), it holds transactional mutations with real
  business rules -- merges, renames, date overrides. Their transaction
  boundaries are this layer's responsibility, not the HTTP handler's.
"""
