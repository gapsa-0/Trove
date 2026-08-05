"""The two searches Browse offers: by description, and by what a file says.

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
    """Document-text index state, and whether this archive can search inside documents.

    The same ``configured``/``enabled`` split ``semantic_status`` draws, for the
    same reason: an archive that declined Documents must not be shown a box that
    searches an index nothing will ever write. Here ``configured`` also covers
    the one dependency that can genuinely be missing -- a SQLite without FTS5 --
    which is a property of the build rather than of the choice.
    """
    from ...services import documents

    rid = req.root_id
    status = text_search.text_summary(req.db(rid), rid)
    # Spelled out rather than imported, the way the catalogue spells out stage
    # kinds; tests/unit/test_features.py checks the id still exists.
    enabled = "documents" in req.cfg.archive_features(rid)
    status["enabled"] = enabled
    status["configured"] = enabled and documents.available(frozenset({"documents"}))
    return status


def text_search_route(req: Request) -> MediaPage | Json:
    """Full-text search over the text read out of documents, ranked by BM25."""
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
