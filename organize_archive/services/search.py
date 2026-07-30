"""Semantic Browse search: index-status reporting for the controls, and the
vector-ranking query itself.

Read side only. Deciding what gets embedded and recording the result is
`services/semantic.py`'s job; this module just ranks the vectors that are
already stored.
"""

from __future__ import annotations

import math
import os

from ..db import database as db
from ._common import _HAS_LOCATION, _NOT_HIDDEN, _quality_ok, _root_clause

# How much a locally translated rewording of a search is discounted against the
# words the user actually typed, so the two only swap places on a clear win.
# Sized against the *embedder's* similarity scale: SigLIP's cosines are far more
# compressed than a retrieval-tuned cloud model's, so the 0.01 that was a
# rounding error before would now be a real handicap. Kept at roughly the same
# fraction of semantic_search_min_similarity as it was then.
ALTERNATE_VECTOR_PENALTY = 0.002

# How many stored vectors semantic_search scores per round trip. The relative
# floor means *every* candidate has to be scored before anything can be cut, so
# this is a memory knob, not a shortcut: at archive scale the embedding column is
# ~200 MB, and reading it all in at once alongside the matrix built from it is
# the one part of search that would actually strain a small machine.
_SCORE_CHUNK = 4096


def semantic_summary(db_path: str, root_id=None) -> dict:
    """Index state for the Browse semantic-search controls."""
    from . import semantic

    conn = db.open_readonly(db_path)
    try:
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
    finally:
        conn.close()


def semantic_pending(db_path: str, root_id=None) -> int:
    """Compatible media that needs a first (or revised) semantic embedding."""
    from . import semantic

    conn = db.open_readonly(db_path)
    try:
        rc, rp = _root_clause(root_id)
        return conn.execute(
            f"""SELECT COUNT(*) FROM files f
                LEFT JOIN semantic_embeddings e ON e.file_id=f.id
                WHERE {_NOT_HIDDEN}{rc}
                  AND (f.media_type='image' OR f.ext IN ('mp4','mov','mp3','wav','pdf'))
                  AND (e.file_id IS NULL OR e.source_sha256 IS NOT f.sha256
                       OR COALESCE(e.indexer_version, '') != ?)""",
            (*rp, semantic.INDEXER_VERSION),
        ).fetchone()[0]
    finally:
        conn.close()


def semantic_search(
    db_path: str,
    query_vector: list[float],
    *,
    root_id=None,
    year=None,
    month=None,
    mtype=None,
    person_id=None,
    person_ids=None,
    cluster_id=None,
    min_similarity=-1.0,
    relative_floor=0.0,
    sort="relevance",
    limit=120,
    offset=0,
    alternate_vectors=None,
    located=None,
) -> dict:
    """Rank locally stored vectors against the original and optional expansions.

    Alternate vectors are ``(vector, penalty)`` pairs.  Taking the best score
    preserves strong matches from either wording; the small penalty keeps the
    words the user actually typed ahead when both formulations are equivalent.
    The penalty is in the units of the *embedder's* similarity scale, so it has
    to be sized against that scale rather than carried over from another model
    (see ``ALTERNATE_VECTOR_PENALTY``).

    **Two cuts, because one absolute number cannot do this job.** The local
    embedder's similarities are compressed *and* shift per query: measured on
    this archive, the best match for "fireworks" scores 0.097 while the best for
    "a selfie" scores 0.150 — and a subject the archive does not contain at all
    still reaches ~0.10. So ``min_similarity`` is only a floor against noise,
    and the cut that actually decides relevance is ``relative_floor``: a
    fraction of *this query's own* best score, which travels with the query
    instead of assuming every query lives on the same scale.
    """
    conn = db.open_readonly(db_path)
    try:
        where = [_NOT_HIDDEN, "e.status='indexed'", "e.embedding IS NOT NULL"]
        params: list = []
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
        vectors = [(tuple(query_vector), 0.0)]
        vectors.extend(
            (tuple(vector), float(penalty)) for vector, penalty in (alternate_vectors or [])
        )
        prepared = [
            (vector, math.sqrt(math.sumprod(vector, vector)), penalty)
            for vector, penalty in vectors
        ]
        prepared = [item for item in prepared if item[1]]
        if not prepared:
            return {"items": [], "offset": offset, "limit": limit, "count": 0, "total": 0}
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
        meta: list[dict] = []
        blocks: list = []
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
        items = [{**item, "score": round(score, 4)} for score, item in page]
        return {
            "items": items,
            "offset": offset,
            "limit": limit,
            "count": len(items),
            "total": len(ranked),
        }
    finally:
        conn.close()
