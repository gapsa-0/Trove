"""Searching the text read out of documents, and reporting how much is readable.

Read side only. Deciding what gets read and recording it is
``services/documents.py``'s job; this ranks what is already indexed.

Ranking is BM25, straight out of FTS5. It is not comparable to the cosine
``services/search.py`` produces and is never mixed with it: the two answer
different questions -- which file *says* this, and which photo *looks like* this
-- and Browse shows them as two labelled groups rather than one list pretending
to a shared scale.
"""

from __future__ import annotations

import os
import re
import sqlite3
from typing import Any, cast

from ..db import database as db
from ._common import _HAS_LOCATION, _NOT_HIDDEN, _root_clause, reading
from .types import MediaItem, MediaPage

# What the snippet is wrapped in. The frontend inserts this as markup, so it has
# to be something no document can contain -- FTS5 does not escape the text it
# returns around the match, which is why the caller escapes it instead.
MARK_OPEN = "\x02"
MARK_CLOSE = "\x03"
ELLIPSIS = "…"

# How many words of context a snippet carries around the match. FTS5 counts
# tokens, not characters; 12 is about a line and a half of prose.
_SNIPPET_TOKENS = 12

# How much of a passage to show when there is no literal match to centre on --
# a document the vectors alone found. Characters rather than tokens, since
# there is no match to count outwards from.
_EXCERPT_CHARS = 200

# Word-ish runs, in any script. Everything else in a query is dropped rather
# than escaped: FTS5's query language treats bare punctuation as syntax, and
# almost every stray character is a hard error rather than a bad result --
# "foo\"bar" is an unterminated string, "a:b" is a missing column, "-x" is a
# missing column, "NOT x" is an operator. A user typing a filename with a dot in
# it is not writing a query language.
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def match_expression(query: str) -> str:
    """Turn typed text into an FTS5 MATCH expression, or "" if there is nothing to search.

    Every token is quoted (so it cannot be read as syntax) and given a prefix
    star. The star is what makes the search behave the way a Spanish or English
    speaker expects: "contrato" finds "contratos", "factura" finds "facturas".
    Without it FTS5 matches whole tokens only, and a plural silently misses.

    Tokens are joined by a space, which is FTS5's implicit AND: a document has
    to contain all of them, which is what "narrow it down by typing more" means.
    """
    tokens = _TOKEN.findall(query or "")
    return " ".join(f'"{token}"*' for token in tokens)


@reading
def text_summary(conn: sqlite3.Connection, root_id: int | None = None) -> dict[str, Any]:
    """How much of this archive can be searched by what it says.

    ``total`` counts documents rather than every file: an archive of 150k photos
    and 300 PDFs has 300 things this feature could ever read, and reporting the
    150k as a denominator would make a finished stage look 0.2% done.
    """
    rc, rp = _root_clause(root_id)
    total = conn.execute(
        f"SELECT COUNT(*) FROM files f WHERE {_NOT_HIDDEN}{rc} AND f.media_type='document'",
        rp,
    ).fetchone()[0]
    rows = conn.execute(
        f"""SELECT t.status, COUNT(*) c FROM doc_text t JOIN files f ON f.id=t.file_id
             WHERE {_NOT_HIDDEN}{rc} AND t.source_sha256 IS f.sha256
             GROUP BY t.status""",
        rp,
    ).fetchall()
    counts = {r["status"]: r["c"] for r in rows}
    passages = conn.execute(
        f"""SELECT COUNT(*) FROM doc_chunks c JOIN files f ON f.id=c.file_id
             WHERE {_NOT_HIDDEN}{rc}""",
        rp,
    ).fetchone()[0]
    completed = sum(counts.values())
    return {
        "total": int(total),
        "read": counts.get("extracted", 0),
        "skipped": counts.get("skipped", 0),
        "errors": counts.get("error", 0),
        "pending": max(0, int(total) - completed),
        "passages": int(passages),
    }


def _filters(
    root_id: int | None,
    year: int | str | None,
    month: str | None,
    person_ids: list[int] | None,
    cluster_id: int | None,
) -> tuple[str, list[Any]]:
    """The same narrowing Browse offers, applied to a text search.

    Deliberately no media-type filter: every hit is a document by construction,
    so offering one could only ever empty the result.
    """
    where = [_NOT_HIDDEN]
    params: list[Any] = []
    rc, rp = _root_clause(root_id)
    if rc:
        where.append(rc.removeprefix(" AND "))
        params += rp
    if year:
        where.append("substr(d.best_datetime,1,4)=?")
        params.append(str(year))
    if month:
        where.append("substr(d.best_datetime,1,7)=?")
        params.append(month)
    for person in dict.fromkeys(person_ids or []):
        where.append(
            "f.id IN (SELECT file_id FROM faces WHERE person_id=? "
            "UNION SELECT file_id FROM person_files WHERE person_id=?)"
        )
        params += [person, person]
    if cluster_id:
        where.append("f.id IN (SELECT file_id FROM place_cluster_members WHERE cluster_id=?)")
        params.append(cluster_id)
    return " AND ".join(where), params


# BM25 ranking only: metadata and snippets are fetched afterwards, for whichever
# files survive the fusion. Collapsing to one row per file has to happen outside
# the MATCH query, because bm25() and snippet() cannot be used in the same
# context as a window function.
_BM25_RANKED = """
WITH hits AS (
  SELECT rowid AS chunk_id, bm25(doc_chunk_fts) AS score
    FROM doc_chunk_fts WHERE doc_chunk_fts MATCH ?
)
SELECT file_id, chunk_id FROM (
    SELECT h.chunk_id, h.score AS score, c.file_id,
           ROW_NUMBER() OVER (PARTITION BY c.file_id ORDER BY h.score, c.ordinal) AS rn
      FROM hits h
      JOIN doc_chunks c ON c.id = h.chunk_id
      JOIN files f ON f.id = c.file_id
      LEFT JOIN dates d ON d.file_id = f.id
     WHERE {where}
  ) WHERE rn = 1
 ORDER BY score, file_id
 LIMIT ?
"""

_VECTOR_CANDIDATES = """
SELECT c.id AS chunk_id, c.file_id AS file_id, e.embedding AS embedding
  FROM doc_chunk_embeddings e
  JOIN doc_chunks c ON c.id = e.chunk_id
  JOIN files f ON f.id = c.file_id
  LEFT JOIN dates d ON d.file_id = f.id
 WHERE e.embedder_version = ? AND {where}
"""

# How many stored vectors are scored per round trip. The whole candidate set has
# to be scored before anything can be ranked, so this is a memory knob rather
# than a shortcut -- exactly the role _SCORE_CHUNK plays in search.py.
_SCORE_CHUNK = 4096


def _vector_ranked(
    conn: sqlite3.Connection,
    query_vector: list[float],
    where: str,
    params: list[Any],
    min_similarity: float,
    depth: int,
) -> list[tuple[int, int]]:
    """Files whose best passage is closest in meaning, best first.

    A near cousin of ``search.py:_score_candidates`` and deliberately not shared
    with it: that one also subtracts a modality centre and carries per-vector
    penalties for translated query wordings, neither of which exists here, and
    folding two different jobs into one function to save a matmul would put the
    calibrated image path at risk for no gain.

    **The cut happens here, before fusion.** An RRF score is in reciprocal-rank
    units and has no relation to similarity, so a floor applied afterwards would
    mean nothing at all.
    """
    import numpy as np

    from . import meaning

    query = np.asarray(query_vector, dtype=np.float32)
    query /= max(float(np.linalg.norm(query)), 1e-12)
    width = query.shape[0] * 4

    best: dict[int, tuple[float, int]] = {}
    cursor = conn.execute(
        _VECTOR_CANDIDATES.format(where=where), (meaning.MEANING_VERSION, *params)
    )
    while True:
        batch = cursor.fetchmany(_SCORE_CHUNK)
        if not batch:
            break
        rows = [r for r in batch if r["embedding"] is not None and len(r["embedding"]) == width]
        if not rows:
            continue
        block = np.frombuffer(b"".join(r["embedding"] for r in rows), dtype="<f4")
        scores = block.reshape(len(rows), -1) @ query
        for row, score in zip(rows, scores.tolist(), strict=True):
            if score < min_similarity:
                continue
            # One row per file: a long document would otherwise occupy the top
            # of this ranking with itself, and fusion would then read that as
            # many separate agreements rather than one.
            current = best.get(row["file_id"])
            if current is None or score > current[0]:
                best[row["file_id"]] = (float(score), int(row["chunk_id"]))

    ordered = sorted(best.items(), key=lambda kv: (-kv[1][0], kv[0]))
    return [(file_id, chunk_id) for file_id, (_score, chunk_id) in ordered[:depth]]


def _rrf(rankings: list[list[tuple[int, int]]], k: int) -> list[tuple[int, int]]:
    """Fuse ranked ``(file_id, chunk_id)`` lists by Reciprocal Rank Fusion.

    Each list contributes ``1 / (k + rank)`` to a file's score, so a document
    both rankings place well beats one that only one of them found. Ranks rather
    than scores is the entire point: BM25 and a cosine have no common scale, and
    there is no normalisation of them that means anything -- but "third" and
    "third" are directly comparable.

    The chunk kept is the one from the earliest-ranked list that offered it, so
    a document found by words shows the passage containing them; a document
    found only by meaning shows its closest passage.

    Ties are broken by file id. RRF produces them constantly -- any two files at
    the same rank in one list and absent from the other -- and without a
    deterministic order a page boundary would move between identical requests.
    """
    scores: dict[int, float] = {}
    chunks: dict[int, int] = {}
    for ranking in rankings:
        for rank, (file_id, chunk_id) in enumerate(ranking, start=1):
            scores[file_id] = scores.get(file_id, 0.0) + 1.0 / (k + rank)
            chunks.setdefault(file_id, chunk_id)
    order = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return [(file_id, chunks[file_id]) for file_id, _score in order]


_ORDERS = {
    "newest": "dt IS NULL, dt DESC, file_id DESC",
    "oldest": "dt IS NULL, dt ASC, file_id ASC",
}
# BM25 returns a negative number, more negative being a better match, so
# ascending is best-first. file_id breaks ties so paging is stable across
# requests rather than depending on how SQLite happened to order equal scores.
_RELEVANCE = "score, file_id"


def _empty(offset: int, limit: int) -> MediaPage:
    return {"items": [], "offset": offset, "limit": limit, "count": 0, "total": 0}


_HYDRATE = """
SELECT c.id AS chunk_id, c.page_first, c.page_last,
       f.id AS file_id, f.media_type, f.rel_path,
       d.best_datetime AS dt, d.date_source AS dsrc, {has_location} AS has_gps
  FROM doc_chunks c
  JOIN files f ON f.id = c.file_id
  LEFT JOIN dates d ON d.file_id = f.id
 WHERE c.id IN ({places})
"""


def _snippets(conn: sqlite3.Connection, chunk_ids: list[int], expression: str) -> dict[int, str]:
    """The passage to show for each chunk, with any literal match marked.

    Two sources, because only one of them can exist for a given hit.
    ``snippet()`` needs a MATCH context, so it can only speak for passages the
    words actually matched; a document the vectors alone found has nothing
    literal to highlight, and gets a plain opening excerpt instead. Showing it
    unmarked is the honest rendering -- there is no matched word in it.
    """
    if not chunk_ids:
        return {}
    places = ",".join("?" for _ in chunk_ids)
    out: dict[int, str] = {}
    if expression:
        rows = conn.execute(
            f"""SELECT rowid AS chunk_id, snippet(doc_chunk_fts, 0, ?, ?, ?, ?) AS snip
                  FROM doc_chunk_fts
                 WHERE doc_chunk_fts MATCH ? AND rowid IN ({places})""",
            (MARK_OPEN, MARK_CLOSE, ELLIPSIS, _SNIPPET_TOKENS, expression, *chunk_ids),
        ).fetchall()
        out.update({int(r["chunk_id"]): r["snip"] for r in rows})
    missing = [c for c in chunk_ids if c not in out]
    if missing:
        rows = conn.execute(
            f"SELECT rowid AS chunk_id, substr(text, 1, ?) AS body FROM doc_chunk_fts "
            f"WHERE rowid IN ({','.join('?' for _ in missing)})",
            (_EXCERPT_CHARS, *missing),
        ).fetchall()
        out.update({int(r["chunk_id"]): (r["body"] or "").strip() + ELLIPSIS for r in rows})
    return out


def _page(
    conn: sqlite3.Connection,
    fused: list[tuple[int, int]],
    expression: str,
    sort: str,
    offset: int,
    limit: int,
) -> list[MediaItem]:
    """Turn one slice of the fused ranking into rendered results.

    Sorting by date has to happen over the whole fused set rather than over the
    slice, so ``newest`` means the newest of everything that matched and not the
    newest of the first screenful. It reorders the same set; it never widens it.
    """
    by_chunk = {chunk_id: file_id for file_id, chunk_id in fused}
    if sort in _ORDERS and fused:
        dates = dict(
            conn.execute(
                f"""SELECT c.id, d.best_datetime FROM doc_chunks c
                      LEFT JOIN dates d ON d.file_id = c.file_id
                     WHERE c.id IN ({",".join("?" for _ in by_chunk)})""",
                list(by_chunk),
            ).fetchall()
        )
        newest = sort == "newest"
        fused = sorted(
            fused,
            key=lambda pair: (dates.get(pair[1]) is None, dates.get(pair[1]) or "", pair[0]),
            reverse=newest,
        )
        # `reverse` would also flip the "undated last" rule, so put it back.
        if newest:
            fused = [p for p in fused if dates.get(p[1])] + [
                p for p in fused if not dates.get(p[1])
            ]

    window = fused[offset : offset + limit]
    if not window:
        return []
    chunk_ids = [chunk_id for _file_id, chunk_id in window]
    snippets = _snippets(conn, chunk_ids, expression)
    rows = {
        int(r["chunk_id"]): r
        for r in conn.execute(
            _HYDRATE.format(has_location=_HAS_LOCATION, places=",".join("?" for _ in chunk_ids)),
            chunk_ids,
        )
    }
    items: list[MediaItem] = []
    for _file_id, chunk_id in window:
        row = rows.get(chunk_id)
        if row is None:
            continue
        items.append(
            cast(
                MediaItem,
                {
                    "id": row["file_id"],
                    "type": row["media_type"],
                    "name": os.path.basename(row["rel_path"]),
                    "date": row["dt"],
                    "date_source": row["dsrc"],
                    "has_gps": bool(row["has_gps"]),
                    "snippet": snippets.get(chunk_id, ""),
                    "page": row["page_first"],
                    "page_last": row["page_last"],
                },
            )
        )
    return items


@reading
def text_search(
    conn: sqlite3.Connection,
    query: str,
    *,
    query_vector: list[float] | None = None,
    root_id: int | None = None,
    year: int | str | None = None,
    month: str | None = None,
    person_ids: list[int] | None = None,
    cluster_id: int | None = None,
    min_similarity: float = 0.82,
    fuse_depth: int = 500,
    rrf_k: int = 60,
    sort: str = "relevance",
    limit: int = 120,
    offset: int = 0,
) -> MediaPage:
    """Files whose text matches, by word and by meaning, fused into one ranking.

    Two rankings go in and one comes out. The words half is FTS5's BM25; the
    meaning half is a cosine over the stored passage vectors, and is used only
    when ``query_vector`` is given -- an archive without Search by meaning, or an
    install without numpy, simply gets the first half and nothing about the
    result shape changes.

    **One row per file, not per passage**, in both halves and before they meet. A
    forty-page contract mentioning the word on thirty of them would otherwise
    fill the first page of results with itself, and fusion would read that as
    thirty separate agreements rather than one.

    Returns an empty page rather than raising when there is nothing to search
    with, or when this build has no FTS5 -- neither is something a user can act
    on differently.
    """
    if not db.text_index_present(conn):
        return _empty(offset, limit)
    expression = match_expression(query)
    if not expression and query_vector is None:
        return _empty(offset, limit)

    where, params = _filters(root_id, year, month, person_ids, cluster_id)
    rankings: list[list[tuple[int, int]]] = []
    if expression:
        rankings.append(
            [
                (int(r["file_id"]), int(r["chunk_id"]))
                for r in conn.execute(
                    _BM25_RANKED.format(where=where), (expression, *params, fuse_depth)
                )
            ]
        )
    if query_vector is not None:
        rankings.append(
            _vector_ranked(conn, query_vector, where, params, min_similarity, fuse_depth)
        )

    fused = _rrf(rankings, rrf_k) if len(rankings) > 1 else (rankings[0] if rankings else [])
    return {
        "items": _page(conn, fused, expression, sort, offset, limit),
        "offset": offset,
        "limit": limit,
        "count": len(fused[offset : offset + limit]),
        "total": len(fused),
    }
