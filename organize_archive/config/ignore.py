"""Extensions, filenames and substrings excluded from the media catalog.

Google Takeout ``.json`` sidecars are excluded here as content but are still
read as metadata elsewhere (see ``metadata/takeout.py``).
"""

from __future__ import annotations

# Files that are not media content. Google Takeout ``.json`` sidecars are
# excluded here as *content* — they are consumed as metadata in Phase 3.
IGNORE_EXTENSIONS = {
    "json",
    "db",
    "thm",
    "ini",
    "nomedia",
    "part",
    "tmp",
}
IGNORE_FILENAMES = {
    "thumbs.db",
    "desktop.ini",
    ".nomedia",
    ".picasa.ini",
    "picasa.ini",
    ".ds_store",
}
# Substrings that mark Google/Picasa/Android index & housekeeping leftovers
# (these often have no extension, e.g. "thumbdata3-123", "nomedia_1620517712...").
IGNORE_NAME_SUBSTRINGS = (
    "thumbindex",
    "thumbdata",
    "database_uuid",
    "nomedia",
    ".com.google.chrome.",  # browser temp download leftovers
)
