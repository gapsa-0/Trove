"""Response shapes shared across `services/` modules -- the app's real
contract with the frontend, so a renamed or dropped key is a checked mypy
error instead of a blank field showing up silently in the GUI.

What belongs here: dict shapes that more than one service module builds (or
that cross the HTTP boundary as a page of results), starting with the
media-grid item every browse-like view returns.

`MediaPage` is declared all the way out at the two route functions that
return it, not just inside `services/`. That works only because `Handler` is
typed `Callable[[Request], object]`: mypy does **not** accept a TypedDict
where `dict[str, Any]` is asked for (a dict's value type is invariant), so a
route still declaring `-> dict` would reject its own service's return.

What does NOT belong here: the ~69 per-mutation result dicts (`{"ok": True,
...}` / `{"error": "..."}`) scattered across the write endpoints. Their
payload keys are all different from one mutation to the next, so a
`total=False` TypedDict would still reject every extra key -- there is no
shape worth naming there. They stay `dict[str, Any]`.
"""

from __future__ import annotations

from typing import TypedDict


class _MediaItemBase(TypedDict):
    """Keys every media-grid item builder fills in."""

    id: int
    type: str
    name: str
    date: str | None
    has_gps: bool


class MediaItem(_MediaItemBase, total=False):
    """One media-grid item, as built by browse, people, places, pets and search."""

    # Keys only some of the five builders add. `date_source` is here, not on
    # the required base, because services/pets.py's pet_group() is the one
    # builder that omits it (and also hardcodes "type": "image" and
    # "has_gps": False, since animal detections carry neither a media type
    # nor a location of their own).
    date_source: str | None
    indexed: bool
    face_id: int | None
    detection_id: int
    score: float
    # Text-search hits only. ``snippet`` is the matching passage with the match
    # marked; the two page fields are the range the passage covers, and are both
    # absent for a format that has no pages (a .txt, a spreadsheet) rather than
    # claiming page 1.
    snippet: str
    page: int | None
    page_last: int | None
    # How a text hit was found, in the two parts Browse labels it with:
    # ``reader`` is the feature whose reader produced the text ("documents" or
    # "ocr"), and ``found_by`` is which half of the fused ranking surfaced it
    # ("words", "meaning" or "both"). They vary independently -- a scanned
    # receipt found by meaning is ocr + meaning.
    reader: str
    found_by: str


class MediaPage(TypedDict):
    """A paginated page of `MediaItem`s, as returned by browse.media() and
    search's semantic_search()."""

    items: list[MediaItem]
    offset: int
    limit: int
    count: int
    total: int
