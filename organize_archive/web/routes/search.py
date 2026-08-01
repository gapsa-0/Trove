"""Semantic (SigLIP) search: index status and the search endpoint itself."""

from __future__ import annotations

from ...services import search
from ._request import Json, Request


def semantic_status(req: Request) -> dict:
    """Semantic index state and whether the local SigLIP model is available."""
    from ...services import semantic

    rid = req.root_id
    status = search.semantic_summary(req.db(rid), rid)
    # Nothing left to configure — the stage runs as soon as the
    # dependencies are importable, and downloads its own weights.
    status["configured"] = semantic.available()
    return status


def semantic_search(req: Request) -> dict | Json:
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

    rid = req.root_id
    db_path = req.db(rid)
    vectors = semantic.embed_queries(req.cfg, search_queries)
    sort_q, located_q = req.one("sort"), req.one("located")
    return search.semantic_search(
        db_path,
        vectors[0],
        root_id=rid,
        year=req.one("year"),
        month=req.one("month"),
        mtype=req.one("type"),
        person_ids=req.many("person"),
        cluster_id=req.one("place", int),
        min_similarity=max(-1.0, min(1.0, float(req.cfg.semantic_search_min_similarity))),
        relative_floor=max(0.0, min(1.0, float(req.cfg.semantic_search_relative_floor))),
        sort=(sort_q if sort_q in ("newest", "oldest") else "relevance"),
        limit=req.limit(120, 500),
        offset=req.offset(),
        located={"yes": True, "no": False}.get(located_q) if located_q is not None else None,
        alternate_vectors=[(vector, search.ALTERNATE_VECTOR_PENALTY) for vector in vectors[1:]],
    )
