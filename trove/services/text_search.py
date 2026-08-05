"""Searching the text read out of documents, and reporting how much is readable.

Read side only. Deciding what gets read and recording it is
``services/documents.py``'s job; this ranks what is already indexed.

Ranking is BM25, straight out of FTS5, and nothing else is mixed into it. It is
not comparable to the cosine ``services/search.py`` produces: the two answer
different questions -- which file *says* this, and which photo *looks like* this
-- and Browse shows them as separate labelled groups rather than one list
pretending to a shared scale. The same goes for the third, which is a plain
filter on file names.

What a hit *is* labelled with is the reader that produced its text
(``reader``), because Documents and Pictures of text write into this one index and
a passage read off a photograph is a best guess where a file's own text is not.
That is a fact about one result rather than about the ranking, which is why it
rides on the row instead of splitting the group.
"""

from __future__ import annotations

import os
import re
import sqlite3
from typing import Any, cast

from ..db import database as db
from ..text import extract
from ..text.results import IMAGE_OCR, PDF_OCR
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
def text_summary(
    conn: sqlite3.Connection,
    root_id: int | None = None,
    *,
    extractors: frozenset[str],
) -> dict[str, Any]:
    """How much of this archive can be searched by what it says.

    ``total`` counts the files the switched-on halves could actually read, not
    every file: an archive of 150k photos and 300 PDFs has 300 things Documents
    could ever open, and reporting the 150k as a denominator would make a
    finished stage look 0.2% done.

    Which is why it is ``readable_exts`` rather than ``media_type='document'``.
    Those two agreed only for as long as Documents was the only reader. Text in
    images reads *pictures*, so an archive that chose it and not Documents used
    to be told it had nothing to read and nothing pending, forever, while the
    pass filled the index behind it. The denominator has to be built from the
    same set the backlog is (``services/documents.py:_candidate_exts``), or the
    two answer different questions.
    """
    rc, rp = _root_clause(root_id)
    exts = sorted(extract.readable_exts(extractors))
    # An empty IN list is a syntax error in SQLite rather than an empty result,
    # so spell "no file" as a false literal. Reached only by a direct call: the
    # route asks for a live feature set.
    ext_sql = f"f.ext IN ({','.join('?' for _ in exts)})" if exts else "0"
    total = conn.execute(
        f"SELECT COUNT(*) FROM files f WHERE {_NOT_HIDDEN}{rc} AND {ext_sql}",
        (*rp, *exts),
    ).fetchone()[0]
    # Scoped to the same extensions as the total above. A file read while the
    # other half was on stays in ``doc_text`` after that half is switched off,
    # and counting it here would report more files read than there are files to
    # read -- and drive ``pending`` to zero while the backlog was not empty.
    rows = conn.execute(
        f"""SELECT t.status, COUNT(*) c FROM doc_text t JOIN files f ON f.id=t.file_id
             WHERE {_NOT_HIDDEN}{rc} AND {ext_sql} AND t.source_sha256 IS f.sha256
             GROUP BY t.status""",
        (*rp, *exts),
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

    Deliberately no media-type filter. It used to be justified by every hit
    being a document by construction, which stopped being true when Text in
    images began writing into these same passages -- a hit can be a photographed
    receipt. The reason now is narrower and still holds: the caller has one
    filter bar for three groups, and a type filter that emptied this one while
    narrowing the others would be one control doing two things.
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


# Ranking, snippet and metadata in one statement, paged by SQL. Collapsing to
# one row per file has to happen outside the MATCH query, because bm25() and
# snippet() cannot be used in the same context as a window function.
_RANKED = """
WITH hits AS (
  SELECT rowid AS chunk_id, bm25(doc_chunk_fts) AS score,
         snippet(doc_chunk_fts, 0, ?, ?, ?, ?) AS snip
    FROM doc_chunk_fts WHERE doc_chunk_fts MATCH ?
)
SELECT file_id, page_first, page_last, snip, media_type, rel_path, extractor,
       dt, dsrc, has_gps
  FROM (
    SELECT h.score, h.snip, c.file_id, c.page_first, c.page_last,
           f.media_type, f.rel_path, t.extractor AS extractor,
           d.best_datetime AS dt, d.date_source AS dsrc,
           {has_location} AS has_gps,
           ROW_NUMBER() OVER (PARTITION BY c.file_id ORDER BY h.score, c.ordinal) AS rn
      FROM hits h
      JOIN doc_chunks c ON c.id = h.chunk_id
      JOIN files f ON f.id = c.file_id
      LEFT JOIN doc_text t ON t.file_id = f.id
      LEFT JOIN dates d ON d.file_id = f.id
     WHERE {where}
  ) WHERE rn = 1
 ORDER BY {order}
 LIMIT ? OFFSET ?
"""

_TOTAL = """
SELECT COUNT(DISTINCT c.file_id)
  FROM doc_chunk_fts
  JOIN doc_chunks c ON c.id = doc_chunk_fts.rowid
  JOIN files f ON f.id = c.file_id
  LEFT JOIN dates d ON d.file_id = f.id
 WHERE doc_chunk_fts MATCH ? AND {where}
"""

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


# Which half of the fused pass produced a file's text, from the extractor
# recorded on its row. Two values rather than six, because this names the
# *feature* a reader belongs to and those are the words the user chose from.
# ``pdf-ocr`` counts as pixels: it is a PDF whose pages had to be looked at
# rather than decoded, so saying "text in pictures" is what happened.
_PIXEL_EXTRACTORS = frozenset({IMAGE_OCR, PDF_OCR})


def reader_of(extractor: str | None) -> str:
    """The feature whose reader produced this text: ``ocr`` or ``documents``.

    Falls back to Documents for an unrecognised or missing value, which is what
    a row written before this distinction existed looks like -- and the older
    behaviour, since Documents was the only reader there was.
    """
    return "ocr" if extractor in _PIXEL_EXTRACTORS else "documents"


@reading
def text_search(
    conn: sqlite3.Connection,
    query: str,
    *,
    root_id: int | None = None,
    year: int | str | None = None,
    month: str | None = None,
    person_ids: list[int] | None = None,
    cluster_id: int | None = None,
    sort: str = "relevance",
    limit: int = 120,
    offset: int = 0,
) -> MediaPage:
    """Files whose text matches, best passage first, one row per file.

    **One row per file, not per passage.** A forty-page contract mentioning the
    word on thirty of them would otherwise fill the whole first page of results
    with itself. The passage kept is the best-scoring one, and it is what the
    snippet and the page number come from.

    Returns an empty page rather than raising when the query has no searchable
    tokens or this build has no FTS5 -- both mean "nothing to match", and
    neither is something the user can act on differently.
    """
    expression = match_expression(query)
    if not expression or not db.text_index_present(conn):
        return _empty(offset, limit)

    where, params = _filters(root_id, year, month, person_ids, cluster_id)
    total = conn.execute(_TOTAL.format(where=where), (expression, *params)).fetchone()[0]
    rows = conn.execute(
        _RANKED.format(
            where=where, order=_ORDERS.get(sort, _RELEVANCE), has_location=_HAS_LOCATION
        ),
        (MARK_OPEN, MARK_CLOSE, ELLIPSIS, _SNIPPET_TOKENS, expression, *params, limit, offset),
    ).fetchall()

    items: list[MediaItem] = [
        cast(
            MediaItem,
            {
                "id": r["file_id"],
                "type": r["media_type"],
                "name": os.path.basename(r["rel_path"]),
                "date": r["dt"],
                "date_source": r["dsrc"],
                "has_gps": bool(r["has_gps"]),
                "snippet": r["snip"],
                "page": r["page_first"],
                "page_last": r["page_last"],
                # Which reader produced this text. Documents and Pictures of text
                # write into one index, so a hit's reader is a property of the
                # file's ``doc_text`` row rather than of a separate search.
                "reader": reader_of(r["extractor"]),
            },
        )
        for r in rows
    ]
    return {
        "items": items,
        "offset": offset,
        "limit": limit,
        "count": len(items),
        "total": int(total),
    }
