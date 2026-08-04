"""Semantic Browse search: index-status reporting for the controls, and the
vector-ranking query itself.

Read side only. Deciding what gets embedded and recording the result is
`services/semantic.py`'s job; this module just ranks the vectors that are
already stored.
"""

from __future__ import annotations

import importlib.util
import math
import os
import sqlite3
from typing import Any, cast

from ..errors import ModelUnavailableError
from ._common import _HAS_LOCATION, _NOT_HIDDEN, _quality_ok, _root_clause, reading
from .types import MediaItem, MediaPage

# How much a locally translated rewording of a search is discounted against the
# words the user actually typed, so the two only swap places on a clear win.
# Sized against the *embedder's* similarity scale: SigLIP's cosines are far more
# compressed than a retrieval-tuned cloud model's, so the 0.01 that was a
# rounding error before would now be a real handicap. Kept at roughly the same
# fraction of semantic_search_min_similarity as it was then.
ALTERNATE_VECTOR_PENALTY = 0.002


def scoring_available() -> bool:
    """True when the dependency the ranking path needs (numpy) is importable.

    The indexing half has its own probe in ``services/semantic.py``, which
    asks for the whole embedding stack. This is the *read* half, and it needs
    only numpy: a catalogue can hold embeddings written by an installation
    that has since lost its extras, and that is exactly the case this answers.

    ``find_spec`` rather than a try/import so asking the question does not
    import a 40 MB package as a side effect.
    """
    return importlib.util.find_spec("numpy") is not None


# How many stored vectors semantic_search scores per round trip. The relative
# floor means *every* candidate has to be scored before anything can be cut, so
# this is a memory knob, not a shortcut: at archive scale the embedding column is
# ~200 MB, and reading it all in at once alongside the matrix built from it is
# the one part of search that would actually strain a small machine.
_SCORE_CHUNK = 4096

# How many stored vectors the archive centre is estimated from. The mean
# converges far faster than the archive grows: measured against a full 497-file
# mean, a 50-vector sample already agrees to cosine 0.993 and reproduces 98% of
# the top-20 ranking, and 300 reaches 0.9995. Sampling is what keeps centering
# from costing a full-column read on every process, so this is deliberately a
# few thousand rather than "all of them".
_CENTER_SAMPLE = 4096

# Archive centres, keyed by (db path, root, indexer version) -- the last so a
# re-index into a new vector space cannot be scored against the old space's
# mean. Process-local and never invalidated within a run: the mean of a photo
# archive moves far too slowly for a single session to notice, and the cost of
# being slightly stale is a fractionally different score, not a wrong result.
_CENTER_CACHE: dict[tuple[str, int | None, str], list[float] | None] = {}


@reading
def archive_center(conn: sqlite3.Connection, root_id: int | None = None) -> list[float] | None:
    """The mean of this archive's stored image vectors, or None if it has none.

    One half of the modality-gap correction (see ``semantic_search``'s
    ``center``). Estimated from a bounded sample rather than the whole column;
    ``_CENTER_SAMPLE`` explains why that is not an approximation worth worrying
    about. Cached per process.
    """
    from . import semantic

    # The archive's own file path, asked of the connection rather than passed
    # in: @reading hands this function a connection, not the db_path its caller
    # used, and two archives must never share a cache entry.
    main_db = ""
    for _seq, name, file in conn.execute("PRAGMA database_list").fetchall():
        if name == "main":
            main_db = file or ""
            break
    key = (main_db, root_id, semantic.INDEXER_VERSION)
    if key in _CENTER_CACHE:
        return _CENTER_CACHE[key]
    import numpy as np

    rc, rp = _root_clause(root_id)
    rows = conn.execute(
        f"""SELECT e.embedding, e.dimensions FROM semantic_embeddings e
            JOIN files f ON f.id=e.file_id
            WHERE {_NOT_HIDDEN}{rc} AND e.status='indexed' AND e.embedding IS NOT NULL
              AND COALESCE(e.indexer_version, '')=?
            LIMIT ?""",
        (*rp, semantic.INDEXER_VERSION, _CENTER_SAMPLE),
    ).fetchall()
    vectors = [
        np.frombuffer(row["embedding"], dtype="<f4")
        for row in rows
        if row["embedding"] is not None and len(row["embedding"]) == (row["dimensions"] or 0) * 4
    ]
    if not vectors:
        _CENTER_CACHE[key] = None
        return None
    block = np.asarray(vectors, dtype=np.float32)
    norms = np.sqrt(np.einsum("ij,ij->i", block, block))
    block = block[norms > 0.0] / norms[norms > 0.0][:, None]
    center = [float(v) for v in block.mean(axis=0)] if len(block) else None
    _CENTER_CACHE[key] = center
    return center


@reading
def semantic_summary(conn: sqlite3.Connection, root_id: int | None = None) -> dict[str, Any]:
    """Index state for the Browse semantic-search controls."""
    from . import semantic

    rc, rp = _root_clause(root_id)
    total = conn.execute(
        f"""SELECT COUNT(*) FROM files f WHERE {_NOT_HIDDEN}{rc}
            AND f.media_type IN ('image','video','audio','document')""",
        rp,
    ).fetchone()[0]
    rows = conn.execute(
        f"""SELECT e.status, COUNT(*) c FROM semantic_embeddings e
            JOIN files f ON f.id=e.file_id
            WHERE {_NOT_HIDDEN}{rc} AND e.source_sha256=f.sha256
              AND COALESCE(e.indexer_version, '')=?
            GROUP BY e.status""",
        (*rp, semantic.INDEXER_VERSION),
    ).fetchall()
    counts = {r["status"]: r["c"] for r in rows}
    completed = sum(counts.values())
    # Per media-type tally of files that actually carry a current embedding
    # (status='indexed'), so Browse can report its search reach as
    # "N images, M videos" rather than a single opaque total.
    type_rows = conn.execute(
        f"""SELECT f.media_type mt, COUNT(*) c FROM semantic_embeddings e
            JOIN files f ON f.id=e.file_id
            WHERE {_NOT_HIDDEN}{rc} AND e.source_sha256=f.sha256
              AND COALESCE(e.indexer_version, '')=? AND e.status='indexed'
            GROUP BY f.media_type ORDER BY c DESC""",
        (*rp, semantic.INDEXER_VERSION),
    ).fetchall()
    by_type = [{"type": r["mt"], "count": r["c"]} for r in type_rows]
    return {
        "total": total,
        "indexed": counts.get("indexed", 0),
        "skipped": counts.get("skipped", 0),
        "errors": counts.get("error", 0),
        "pending": max(0, total - completed),
        "by_type": by_type,
    }


@reading
def semantic_pending(conn: sqlite3.Connection, root_id: int | None = None) -> int:
    """Compatible media that needs a first (or revised) semantic embedding."""
    from . import semantic

    rc, rp = _root_clause(root_id)
    # int() around the fetched value is a no-op at runtime -- COUNT(*) always
    # returns one row holding an int -- but sqlite3.Row.__getitem__ is typed
    # Any, which the `-> int` would otherwise silently swallow (see pending.py).
    row = conn.execute(
        f"""SELECT COUNT(*) FROM files f
            LEFT JOIN semantic_embeddings e ON e.file_id=f.id
            WHERE {_NOT_HIDDEN}{rc}
              AND (f.media_type='image' OR f.ext IN ('mp4','mov','mp3','wav','pdf'))
              AND (e.file_id IS NULL OR e.source_sha256 IS NOT f.sha256
                   OR COALESCE(e.indexer_version, '') != ?)""",
        (*rp, semantic.INDEXER_VERSION),
    ).fetchone()
    return int(row[0])


def _search_where_clause(
    root_id: int | None,
    year: int | str | None,
    month: str | None,
    mtype: str | None,
    person_id: int | None,
    person_ids: list[int] | None,
    cluster_id: int | None,
    located: bool | None,
) -> tuple[str, list[Any]]:
    """Build the WHERE clause and SQL text for semantic_search's candidate query."""
    where = [_NOT_HIDDEN, "e.status='indexed'", "e.embedding IS NOT NULL"]
    params: list[Any] = []
    rc, rp = _root_clause(root_id)
    if rc:
        where.append(rc.removeprefix(" AND "))
        params += rp
    if mtype:
        where.append("f.media_type=?")
        params.append(mtype)
    if year:
        where.append("substr(d.best_datetime,1,4)=?")
        params.append(str(year))
    if month:
        where.append("substr(d.best_datetime,1,7)=?")
        params.append(month)
    selected_people = list(dict.fromkeys(person_ids or ([person_id] if person_id else [])))
    for selected_person in selected_people:
        # Mirrors media()'s person filter: union in person_files so
        # manually tagged files (no detected face) match too.
        where.append(
            f"f.id IN (SELECT file_id FROM faces WHERE person_id=? "
            f"AND {_quality_ok('faces')} "
            "UNION SELECT file_id FROM person_files WHERE person_id=?)"
        )
        params.append(selected_person)
        params.append(selected_person)
    if cluster_id:
        where.append("f.id IN (SELECT file_id FROM place_cluster_members WHERE cluster_id=?)")
        params.append(cluster_id)
    # Unlike the indexed box -- which cannot narrow a set that is all indexed
    # by definition -- a location filter is meaningful here, so a description
    # search accepts it rather than making the box go dead while searching.
    if located is not None:
        where.append(_HAS_LOCATION if located else f"NOT {_HAS_LOCATION}")
    sql = f"""SELECT f.id, f.media_type, f.rel_path, d.best_datetime AS dt,
                     d.date_source AS dsrc,
                     {_HAS_LOCATION} AS has_gps,
                     e.embedding
              FROM semantic_embeddings e JOIN files f ON f.id=e.file_id
              LEFT JOIN dates d ON d.file_id=f.id
              WHERE {" AND ".join(where)}"""
    return sql, params


def _prepare_query_vectors(
    query_vector: list[float],
    alternate_vectors: list[tuple[list[float], float]] | None,
    text_center: list[float] | None = None,
) -> list[tuple[tuple[float, ...], float, float]]:
    """Turn the query vector(s) into ``(vector, norm, penalty)`` triples, dropping
    any that come out zero-norm.

    ``text_center`` is the text modality's mean, subtracted here when centering
    is on. Renormalising is left to the caller's existing division by ``norm``,
    which is computed after the shift.
    """
    vectors = [(tuple(query_vector), 0.0)]
    vectors.extend((tuple(vector), float(penalty)) for vector, penalty in (alternate_vectors or []))
    if text_center is not None and len(text_center) == len(query_vector):
        centered = [
            (tuple(value - mean for value, mean in zip(vector, text_center, strict=True)), penalty)
            for vector, penalty in vectors
        ]
        vectors = centered
    prepared = [
        (vector, math.sqrt(math.sumprod(vector, vector)), penalty) for vector, penalty in vectors
    ]
    prepared = [item for item in prepared if item[1]]
    return prepared


def _score_candidates(
    conn: sqlite3.Connection,
    sql: str,
    params: list[Any],
    prepared: list[tuple[tuple[float, ...], float, float]],
    image_center: Any = None,
) -> tuple[Any, list[MediaItem]]:
    """Run the candidate query and score every row against ``prepared``.

    Returns ``(scores, meta)``: a float32 array of best-of-``prepared`` scores
    and the parallel per-row metadata dicts, both in the query's row order.

    ``image_center`` is the image modality's mean (a float32 array or None),
    subtracted from every candidate before scoring. Renormalising is free: the
    per-row division by ``norms`` below already happens after the shift.
    """
    # numpy stays a function-local import on purpose: hoisting it would make
    # importing this module require numpy even for callers that never touch a
    # score. That means the array types below can't be spelled `np.ndarray` at
    # module scope either -- `Any` is the honest type for anything that
    # crosses this function's boundary as one of numpy's arrays.
    import numpy as np

    dim = len(prepared[0][0])
    blob_bytes = dim * 4
    query_matrix = np.array([v for v, _n, _p in prepared], dtype=np.float32)
    query_norms = np.array([n for _v, n, _p in prepared], dtype=np.float32)
    penalties = np.array([p for _v, _n, p in prepared], dtype=np.float32)
    # One matmul per chunk instead of a Python loop per row: the same
    # arithmetic, but the inner product moves out of the interpreter. Each
    # chunk is scored and dropped, so only the per-row metadata and one
    # float32 score per row outlive the loop -- not the blobs themselves.
    meta: list[MediaItem] = []
    blocks: list[Any] = []
    cursor = conn.execute(sql, params)
    while True:
        batch = cursor.fetchmany(_SCORE_CHUNK)
        if not batch:
            break
        block = np.empty((len(batch), dim), dtype=np.float32)
        kept = 0
        for row in batch:
            blob = row["embedding"]
            # A blob of a different width came from a different embedder, so
            # it is not comparable to this query: skipped rather than scored.
            # This is what lets an archive re-index itself in place after a
            # model change, instead of needing its old vectors deleted first.
            if blob is None or len(blob) != blob_bytes:
                continue
            block[kept] = np.frombuffer(blob, dtype="<f4")
            meta.append(
                {
                    "id": row["id"],
                    "type": row["media_type"],
                    "name": os.path.basename(row["rel_path"]),
                    "date": row["dt"],
                    "date_source": row["dsrc"],
                    "has_gps": bool(row["has_gps"]),
                }
            )
            kept += 1
        if not kept:
            continue
        block = block[:kept]
        if image_center is not None:
            block = block - image_center
        norms = np.sqrt(np.einsum("ij,ij->i", block, block))
        # A zero vector points nowhere, so there is no angle to measure.
        # Dividing by 1 keeps the arithmetic finite and -inf then puts it
        # below any finite min_similarity, matching the old explicit skip.
        scores_block = block @ query_matrix.T
        scores_block /= np.where(norms > 0.0, norms, np.float32(1.0))[:, None]
        scores_block /= query_norms[None, :]
        scores_block -= penalties[None, :]
        best_per_row = scores_block.max(axis=1)
        best_per_row[norms <= 0.0] = -np.inf
        blocks.append(best_per_row)
    scores = np.concatenate(blocks) if blocks else np.zeros(0, dtype=np.float32)
    return scores, meta


def _apply_similarity_cuts(scores: Any, min_similarity: float, relative_floor: float) -> Any:
    """Return the indices of ``scores`` that clear the absolute and relative floors.

    ``scores``/the return value are numpy arrays; see ``_score_candidates`` for
    why they are typed ``Any`` instead of ``np.ndarray`` here.
    """
    import numpy as np

    keep = np.flatnonzero(scores >= min_similarity)
    # The relative cut needs the best score, so it can only be applied once
    # every candidate has been scored. Skipped when the best match is itself
    # negative: scaling a negative number by a fraction *raises* the bar
    # towards zero, which would throw away the whole (already poor) result
    # set rather than trim it.
    if relative_floor > 0.0 and keep.size:
        best = float(scores[keep].max())
        if best > 0.0:
            keep = keep[scores[keep] >= relative_floor * best]
    return keep


def _rank_and_paginate(
    scores: Any,
    meta: list[MediaItem],
    keep: Any,
    sort: str,
    offset: int,
    limit: int,
) -> MediaPage:
    """Sort the kept candidates and slice out one page as the response dict.

    ``scores``/``keep`` are numpy arrays; see ``_score_candidates`` for why
    they are typed ``Any`` instead of ``np.ndarray`` here.
    """
    # Ascending index order, which is the SQL's order -- so the sorts below
    # stay stable against it exactly as they were over the old scored list.
    ranked = [(float(scores[i]), meta[i]) for i in keep]
    # Which matched is decided by similarity; how they are ordered is the
    # caller's choice.  A date sort still shows only what cleared
    # min_similarity -- it reorders the same result set, never widens it.
    # Undated files sort last either way, as in media().
    if sort == "oldest":
        ranked.sort(key=lambda x: (x[1]["date"] is None, x[1]["date"] or "", x[1]["id"]))
    elif sort == "newest":
        ranked.sort(key=lambda x: (x[1]["date"] or "", -x[1]["id"]), reverse=True)
    else:
        ranked.sort(key=lambda x: x[0], reverse=True)
    page = ranked[offset : offset + limit]
    # mypy widens a TypedDict under **-unpacking to plain dict[str, Any], so it
    # cannot see that this is still a MediaItem plus its one optional `score`.
    # The cast says so rather than rebuilding each dict field by field.
    items: list[MediaItem] = [
        cast(MediaItem, {**item, "score": round(score, 4)}) for score, item in page
    ]
    return {
        "items": items,
        "offset": offset,
        "limit": limit,
        "count": len(items),
        "total": len(ranked),
    }


@reading
def semantic_search(
    conn: sqlite3.Connection,
    query_vector: list[float],
    *,
    root_id: int | None = None,
    year: int | str | None = None,
    month: str | None = None,
    mtype: str | None = None,
    person_id: int | None = None,
    person_ids: list[int] | None = None,
    cluster_id: int | None = None,
    min_similarity: float = -1.0,
    relative_floor: float = 0.0,
    sort: str = "relevance",
    limit: int = 120,
    offset: int = 0,
    alternate_vectors: list[tuple[list[float], float]] | None = None,
    located: bool | None = None,
    center: tuple[list[float], list[float]] | None = None,
) -> MediaPage:
    """Rank locally stored vectors against the original and optional expansions.

    Alternate vectors are ``(vector, penalty)`` pairs.  Taking the best score
    preserves strong matches from either wording; the small penalty keeps the
    words the user actually typed ahead when both formulations are equivalent.
    The penalty is in the units of the *embedder's* similarity scale, so it has
    to be sized against that scale rather than carried over from another model
    (see ``ALTERNATE_VECTOR_PENALTY``).

    **Two cuts, because one absolute number cannot do this job.** The local
    embedder's similarities are compressed *and* shift per query, so the two
    populations overlap: "a dog" tops out at 0.0916 on an archive full of dogs
    while "the surface of mars", which it holds none of, reaches 0.0948. The
    cuts survive that by binding on different populations -- ``min_similarity``
    where a query's best score is low (the archive holds nothing like it), and
    ``relative_floor`` as a fraction of *this query's own* best, which travels
    with the query instead of assuming a fixed scale. See ``config.py``.

    ``center`` is ``(image_mean, text_mean)``, enabling modality-gap correction:
    each side is shifted by its own modality's mean before the cosine, which
    collapses the constant offset between the image and text clusters. Passing
    ``None`` leaves the arithmetic exactly as it was. The effect is a wider,
    better-behaved score range rather than a reordering of obvious matches --
    but it does reorder, so the cuts above are tuned per mode (see
    ``Config.semantic_search_center_embeddings``).

    Raises ``ModelUnavailableError`` when numpy is not installed. That is the
    one place this module cannot degrade: ranking *is* the operation, so there
    is no reduced answer to give, and an empty page would read as "your
    archive contains nothing like that" -- the wrong thing to tell someone
    whose install is simply missing an extra. The HTTP layer turns it into a
    400 carrying this message, so the user is told what to install.
    """
    if not scoring_available():
        raise ModelUnavailableError(
            "semantic search needs numpy, which is not installed. "
            "Install the 'semantic' extra to use description search."
        )
    import numpy as np

    sql, params = _search_where_clause(
        root_id, year, month, mtype, person_id, person_ids, cluster_id, located
    )
    image_center, text_center = center if center else (None, None)
    # A centre of the wrong width came from a different vector space; ignoring
    # it degrades to uncentered scoring rather than raising, matching how a
    # stale-width embedding blob is skipped below.
    if image_center is not None and len(image_center) != len(query_vector):
        image_center = text_center = None
    prepared = _prepare_query_vectors(query_vector, alternate_vectors, text_center)
    if not prepared:
        return {"items": [], "offset": offset, "limit": limit, "count": 0, "total": 0}
    scores, meta = _score_candidates(
        conn,
        sql,
        params,
        prepared,
        None if image_center is None else np.asarray(image_center, dtype=np.float32),
    )
    keep = _apply_similarity_cuts(scores, min_similarity, relative_floor)
    return _rank_and_paginate(scores, meta, keep, sort, offset, limit)
