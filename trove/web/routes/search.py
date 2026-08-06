"""Two of the three searches Browse offers: by description, and by what a file
says. The third needs no endpoint of its own -- matching a file's name is a
filter on the plain listing, and ``routes/browse.py`` already serves that.

They are kept apart all the way out to the client. A SigLIP cosine and a BM25
rank are different scales measuring different things, so there is no merged
ranking and no shared endpoint -- Browse asks for whichever groups the archive
has enabled and labels them separately. That also means each degrades on its
own: an install without numpy loses description search and keeps text search,
which needs nothing but SQLite.
"""

from __future__ import annotations

import logging
import threading

from ...errors import ModelUnavailableError
from ...services import search, text_search
from ...services.types import MediaPage
from ._request import Json, Request

logger = logging.getLogger(__name__)


def _warm_archive_center(db_path: str, rid: int | None) -> None:
    """Compute this archive's centre off the request thread, best-effort.

    The model-side warm-up happens once at server start, but the centre is a
    property of *an archive*, and which one is open is not known until someone
    opens it. Library asks for the status above the moment it renders, so that
    is where the archive first becomes knowable — and it is a poll, so this
    lands well before anyone finishes typing a query.

    Silent on failure and never awaited: search recomputes the centre itself if
    this did not happen, so the only thing a failure here costs is the head
    start.
    """

    def run() -> None:
        try:
            search.archive_center(db_path, rid)
        except Exception:
            logger.debug("archive centre warmup failed", exc_info=True)

    threading.Thread(target=run, name="semantic-center-warm", daemon=True).start()


def semantic_status(req: Request) -> dict:
    """Semantic index state, and whether this archive can search by description.

    ``configured`` answers the only question its callers ask -- *will this
    archive ever have anything to search?* -- which takes two facts, not one.
    It used to report solely whether the SigLIP dependencies import, so an
    archive that declined Search by description was told the feature was
    configured and then shown "0 files searchable, none queued" forever: its
    semantic stage is left out of the pipeline entirely (ADR 0015), so nothing
    was ever going to be indexed for it.
    """
    from ...services import semantic

    rid = req.root_id
    db_path = req.db(rid)
    status = search.semantic_summary(db_path, rid)
    # The feature id is spelled out rather than imported, the same way the
    # catalogue spells out stage kinds; tests/unit/test_features.py checks it.
    enabled = "semantic" in req.cfg.archive_features(rid)
    # Beyond being chosen there is nothing to configure: the stage runs as soon
    # as the dependencies are importable, and downloads its own weights.
    status["configured"] = enabled and semantic.available()
    # Kept apart so the client can tell "this archive did not ask for it" from
    # "this build cannot do it" -- one is a choice to undo on the setup screen,
    # the other is an installation that has no such feature to offer.
    status["enabled"] = enabled
    if status["configured"] and status.get("indexed") and req.cfg.semantic_search_center_embeddings:
        _warm_archive_center(db_path, rid)
    return status


def similar(req: Request) -> MediaPage | Json:
    """Files whose picture looks like the one named by ``id``.

    Unlike description search this needs no model at all: the vector is already
    in the catalogue, so an installation that has lost the embedding extras can
    still answer it as long as numpy is importable. Hence no
    ``ModelUnavailableError`` here -- the one failure worth distinguishing is
    "this file has nothing to compare with", which is a 200 saying so rather
    than an error, because it is a normal state of a half-indexed archive.
    """
    raw = req.one("id")
    if raw is None or not raw.isdigit():
        return Json({"error": "A file id is required"}, 400)
    limit = max(1, min(24, int(req.one("limit") or 8)))
    # Same fallback as /api/item/: the viewer knows its archive and names it,
    # but ``req.db(None)`` raises rather than returning nothing, so an omitted
    # ``root`` would surface as a 500 instead of an empty answer.
    rid = req.root_id or req.open_root_id
    if rid is None:
        return Json({"items": [], "indexed": False})
    page = search.similar_media(req.db(rid), int(raw), root_id=rid, limit=limit)
    if page is None:
        return Json({"items": [], "indexed": False})
    return page


def semantic_search(req: Request) -> MediaPage | Json:
    """Free-text semantic search over the archive's media, ranked by embedding similarity."""
    search_queries = []
    for value in req.query.get("q", []):
        value = value.strip()
        if value and value not in search_queries:
            search_queries.append(value)
    # The first query is the user's wording.  At most one locally
    # translated expansion is accepted to keep ranking predictable.
    search_queries = search_queries[:2]
    if not search_queries:
        return Json({"error": "A search query is required"}, 400)

    from ...services import semantic

    # Asked before embedding, not after: embed_queries loads the text tower and
    # would raise ModuleNotFoundError from inside onnxruntime, which reaches the
    # user as a 500 and a traceback. The same question the status endpoint
    # already asks, so the answer cannot disagree with what the UI displays.
    if not semantic.available():
        raise ModelUnavailableError(
            "semantic search needs the local embedding model, which is not "
            "installed. Install the 'semantic' extra to use description search."
        )
    rid = req.root_id
    db_path = req.db(rid)
    vectors = semantic.embed_queries(req.cfg, search_queries)
    sort_q, located_q = req.one("sort"), req.one("located")
    # "top=no" turns both relevance cuts off, ranking the whole archive instead
    # of trimming it. Absence means the cuts apply, so an unchanged URL keeps
    # the tuned behaviour; only a user who deliberately widened the search says
    # so. The cuts exist because a query the archive cannot answer otherwise
    # returns its best near-random matches looking confident, so this is a
    # per-search escape hatch, not a setting worth defaulting to.
    unfiltered = req.one("top") == "no"
    # Modality-gap correction, assembled here because its two halves come from
    # different places: the image mean from this archive's stored vectors, the
    # text mean from the model. Both are plain floats by the time they reach
    # the scorer, which keeps services/search.py needing only numpy. An archive
    # with nothing indexed has no centre, and scoring falls back to uncentered.
    center = None
    if req.cfg.semantic_search_center_embeddings:
        image_center = search.archive_center(db_path, rid)
        if image_center is not None:
            center = (image_center, semantic.text_center(req.cfg))
    return search.semantic_search(
        db_path,
        vectors[0],
        root_id=rid,
        year=req.one("year"),
        month=req.one("month"),
        mtype=req.one("type"),
        person_ids=req.many("person"),
        cluster_id=req.one("place", int),
        min_similarity=(
            -1.0
            if unfiltered
            else max(-1.0, min(1.0, float(req.cfg.semantic_search_min_similarity)))
        ),
        relative_floor=(
            0.0 if unfiltered else max(0.0, min(1.0, float(req.cfg.semantic_search_relative_floor)))
        ),
        sort=(sort_q if sort_q in ("newest", "oldest") else "relevance"),
        limit=req.limit(120, 500),
        offset=req.offset(),
        located={"yes": True, "no": False}.get(located_q) if located_q is not None else None,
        alternate_vectors=[(vector, search.ALTERNATE_VECTOR_PENALTY) for vector in vectors[1:]],
        center=center,
    )


def text_status(req: Request) -> dict:
    """Text-index state, and whether this archive can search what its files say.

    The same ``configured``/``enabled`` split ``semantic_status`` draws, for the
    same reason: an archive that declined both readers must not be shown a box
    that searches an index nothing will ever write. Here ``configured`` also
    covers the one dependency that can genuinely be missing -- a SQLite without
    FTS5 -- which is a property of the build rather than of the choice.

    ``readers`` is the third fact, and the new one: *which* halves are on. Both
    text features write into one index, so a count of what has been read means
    nothing without saying what was being read.
    """
    from ... import features
    from ...services import documents

    rid = req.root_id
    # Which readers this archive switched on, which is both what the summary
    # counts against and what decides the feature is live at all. The two halves
    # are chosen separately and either one alone fills the same index, so asking
    # only about the document half told a picture-only archive its text search did
    # not exist while the pass was filling it.
    extractors = features.extractors(req.cfg.archive_features(rid))
    status = text_search.text_summary(req.db(rid), rid, extractors=extractors)
    status["readers"] = sorted(extractors)
    status["enabled"] = bool(extractors)
    status["configured"] = documents.available(extractors)
    return status


def text_search_route(req: Request) -> MediaPage | Json:
    """Search the text read out of documents and off the writing in pictures."""
    query = (req.one("q") or "").strip()
    if not query:
        return Json({"error": "A search query is required"}, 400)
    rid = req.root_id
    sort_q = req.one("sort")

    return text_search.text_search(
        req.db(rid),
        query,
        root_id=rid,
        year=req.one("year"),
        month=req.one("month"),
        person_ids=req.many("person"),
        cluster_id=req.one("place", int),
        sort=(sort_q if sort_q in ("newest", "oldest") else "relevance"),
        limit=req.limit(120, 500),
        offset=req.offset(),
    )
