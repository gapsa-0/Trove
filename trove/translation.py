"""The local Spanish-to-English translator's four files, named once.

The search box translates a Spanish query to English before embedding it, for
a reason measured rather than assumed: a Spanish query gets hijacked by Spanish
text *rendered inside* images, which a photo archive is full of, and the wrong
results score higher than the right ones. ``web/static/js/search.js`` carries
the numbers.

The translation itself happens in the browser, in a WASM worker, so nothing in
Python ever opens these files -- this module exists only to say which manifest
entries they are. Two callers need that list and sit on opposite sides of the
app: ``services/models`` fetches them alongside the SigLIP towers, and the
``/vendor`` route serves them back to the page out of the cache. Naming them in
either of those would make the other import across a layer it has no business
in, so they are named here, at L0, where both can see them.

They used to sit in ``web/vendor/`` inside the package, which meant everyone
carried 15.9 MB of compressed installer for a translator that only matters once
Search by description is on -- and that feature already downloads 689 MB, where
26 more are invisible. ADR 0019's reasoning about the OCR weights applies
unchanged; this is the same move made for the same reason.
"""

from __future__ import annotations

from pathlib import Path

from . import model_manifest

# Manifest name -> the filename the page asks ``/vendor/`` for. The page's own
# registry (``web/vendor/translation-es-en.json``) spells those URLs, and it
# carries its own copy of the sizes and SHA-256s, so a corrupt file is refused
# on both sides of the wire.
MODELS = {
    "bergamot_wasm": "bergamot-translator-worker.wasm",
    "bergamot_es_en_model": "translate-es-en-model.bin",
    "bergamot_es_en_lex": "translate-es-en-lex.bin",
    "bergamot_es_en_vocab": "translate-es-en-vocab.spm",
}

# The reverse, for a route that has a filename and needs the entry.
BY_FILENAME = {filename: name for name, filename in MODELS.items()}


def ready(cache_dir: str) -> bool:
    """Whether all four are on this machine. Never downloads.

    All four or none, deliberately. Three of them are one model and the fourth
    is the runtime that reads it, so a partial set is not a degraded translator
    but a broken one -- and the page would discover that as a failed fetch
    mid-search rather than as a feature that had not finished installing.
    """
    return all(model_manifest.present(name, cache_dir) is not None for name in MODELS)


def resolve(filename: str, cache_dir: str) -> Path | None:
    """Where a ``/vendor/`` request for one of these should be served from.

    None covers both "not one of ours" and "not downloaded yet". The route
    turns either into a 404, which is the answer the page already knows how to
    take: translation improves recall and is never required, so a missing
    translator costs a Spanish query its expansion and nothing else.

    All four or none here too, for a reason that belongs to the page rather than
    to this module. The search box decides whether a translator exists on this
    machine by asking for one byte of the model file (``search.js``,
    ``translatorPresent``), and then hands the loader a fifteen-second download
    timeout. Serving each file on its own merits would let an interrupted fetch
    answer "yes, the model is here" and then stall every search for the whole of
    that timeout on the runtime that is not -- so a half-downloaded translator
    would be slower than no translator at all. A set that is not complete is not
    a translator, and this says so on the first byte.
    """
    name = BY_FILENAME.get(filename)
    if not name or not ready(cache_dir):
        return None
    return model_manifest.present(name, cache_dir)
