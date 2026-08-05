"""Reading the text that is inside a file, rather than about it.

One package per concern, per the L1 convention: this is the algorithms only.
Given a path it answers "what does this file say", as blocks of text carrying
the page they came from, and it knows nothing about the catalogue, the pipeline,
or which archive asked. Deciding *which* files to read, recording the outcome and
keeping the index in step are ``services/documents.py``'s job; scheduling the
pass is ``pipeline/runners/text.py``'s.

What lives here:

* ``results.py`` -- the vocabulary the whole feature shares: which extractors
  exist, the shape a reading comes back in, and the two skip-reason prefixes that
  decide whether a failure is worth retrying.
* ``extract.py`` -- one entry point, dispatching on extension.
* ``pdf.py`` / ``office.py`` / ``plain.py`` -- one reader per family.
* ``chunk.py`` -- cutting a reading into the passages that get indexed.

What does not: anything that opens a database, anything that names a feature id,
and any wording shown to a user. A skip reason from here is a diagnostic string
the service layer stores; the sentence the panel prints is composed from
``features.py`` like every other.
"""
