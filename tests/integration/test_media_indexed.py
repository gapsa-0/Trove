"""The Browse grid's indexing coverage: the per-tile marker and the "Show only
indexed files" box.

The rule under test is narrow and easy to get wrong: a file counts as indexed
only when a *current* vector describes its *current* bytes. A row that exists but
was written by a previous model, or that describes content the file no longer
has, or that records a skip rather than a vector, cannot answer a query -- and
marking it indexed would promise the user a search result that never comes.
"""

from __future__ import annotations

import struct

from organize_archive.db import database as db
from organize_archive.services import semantic
from organize_archive.web import queries

CURRENT = semantic.INDEXER_VERSION


def _archive(tmp_path, rows, located=()):
    """rows: (file_id, sha, embedding_row or None).

    embedding_row is (source_sha, indexer_version, status) -- everything the
    indexed rule looks at. ``located`` lists the file_ids that get a geo row.
    """
    db_path = tmp_path / "archive.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/photos','2026-01-01')")
    for file_id, sha, emb in rows:
        conn.execute(
            """INSERT INTO files(id,root_id,rel_path,size,mtime,media_type,
                                 sha256,first_seen,last_seen)
               VALUES(?,1,?,1,0,'image',?,'2026-01-01','2026-01-01')""",
            (file_id, f"{file_id}.jpg", sha),
        )
        conn.execute(
            """INSERT INTO dates(file_id,best_datetime,date_source,date_confidence)
               VALUES(?,?,'exif',1.0)""",
            (file_id, f"2026-01-{file_id:02d}T00:00:00"),
        )
        if emb is not None:
            source_sha, version, status = emb
            conn.execute(
                """INSERT INTO semantic_embeddings(
                       file_id,source_sha256,model,dimensions,embedding,status,
                       indexer_version,indexed_at
                   ) VALUES(?,?,'m',2,?,?,?,'2026-01-01')""",
                (file_id, source_sha, struct.pack("<2f", 1.0, 0.0), status, version),
            )
    for file_id in located:
        conn.execute(
            "INSERT INTO geo(file_id,lat,lon,geo_source) VALUES(?,-34.6,-58.4,'exif')", (file_id,)
        )
    conn.commit()
    conn.close()
    return db_path


def _flags(db_path, **kw):
    return {i["id"]: i["indexed"] for i in queries.media(db_path, root_id=1, **kw)["items"]}


def test_only_a_current_vector_of_the_current_bytes_counts(tmp_path):
    """Every way a row can exist without being findable, in one grid."""
    db_path = _archive(
        tmp_path,
        [
            (1, "sha1", ("sha1", CURRENT, "indexed")),  # the real thing
            (2, "sha2", None),  # never indexed
            (3, "sha3", ("sha3", "voyage-mm-3.5-2", "indexed")),  # previous model
            (4, "sha4", ("OLD", CURRENT, "indexed")),  # file changed since
            (5, "sha5", ("sha5", CURRENT, "skipped")),  # deliberately skipped
            (6, "sha6", ("sha6", CURRENT, "error")),  # failed
        ],
    )
    assert _flags(db_path) == {1: True, 2: False, 3: False, 4: False, 5: False, 6: False}


def test_a_stale_model_row_is_not_marked_indexed(tmp_path):
    """The migration case, called out on its own because it is the one that
    would silently mislead: the archive is full of rows from the previous
    embedder, and none of them can answer a query in today's vector space."""
    db_path = _archive(
        tmp_path,
        [
            (1, "sha1", ("sha1", "voyage-mm-3.5-2", "indexed")),
            (2, "sha2", ("sha2", "voyage-mm-3.5-2", "indexed")),
        ],
    )
    assert _flags(db_path) == {1: False, 2: False}
    assert queries.media(db_path, root_id=1, indexed=True)["total"] == 0
    assert queries.browse_filters(db_path, 1)["indexed_any"] is False


def test_the_two_filters_partition_the_grid(tmp_path):
    """Whatever the mix, indexed and not-indexed must together be exactly the
    unfiltered grid -- no file falls through both, none is counted twice. The GUI
    only checks the box (indexed=True), but the two halves summing to the whole is
    what proves the predicate is not quietly dropping rows. `total` matters as
    much as the items: it is the count the grid shows."""
    db_path = _archive(
        tmp_path,
        [
            (1, "sha1", ("sha1", CURRENT, "indexed")),
            (2, "sha2", None),
            (3, "sha3", ("sha3", CURRENT, "indexed")),
            (4, "sha4", ("sha4", "voyage-mm-3.5-2", "indexed")),
        ],
    )
    everything = queries.media(db_path, root_id=1)
    yes = queries.media(db_path, root_id=1, indexed=True)
    no = queries.media(db_path, root_id=1, indexed=False)
    assert yes["total"] + no["total"] == everything["total"] == 4
    assert sorted(i["id"] for i in yes["items"]) == [1, 3]
    assert sorted(i["id"] for i in no["items"]) == [2, 4]
    assert all(i["indexed"] for i in yes["items"])
    assert not any(i["indexed"] for i in no["items"])


def test_a_file_with_no_hash_yet_is_not_indexed(tmp_path):
    """sha256 is NULL until a file has been hashed. The comparison against it is
    then NULL, which must read as "not indexed" rather than throwing the row
    out of both halves of the filter."""
    db_path = _archive(tmp_path, [(1, None, ("sha1", CURRENT, "indexed"))])
    assert _flags(db_path) == {1: False}
    assert queries.media(db_path, root_id=1, indexed=False)["total"] == 1


def test_coverage_composes_with_the_other_filters(tmp_path):
    """The filter bar's premise is that its controls narrow together."""
    db_path = _archive(
        tmp_path,
        [
            (1, "sha1", ("sha1", CURRENT, "indexed")),
            (2, "sha2", ("sha2", CURRENT, "indexed")),
            (3, "sha3", None),
        ],
    )
    both = queries.media(db_path, root_id=1, indexed=True, month="2026-01")
    assert sorted(i["id"] for i in both["items"]) == [1, 2]
    assert queries.media(db_path, root_id=1, indexed=True, month="2025-12")["total"] == 0


def test_the_box_offers_itself_once_anything_is_indexed(tmp_path):
    db_path = _archive(
        tmp_path,
        [
            (1, "sha1", ("sha1", CURRENT, "indexed")),
            (2, "sha2", None),
        ],
    )
    assert queries.browse_filters(db_path, 1)["indexed_any"] is True


# -- location coverage ------------------------------------------------------
#
# The 📍 badge's filter. Same shape as the indexed box deliberately, but on a
# different fact, so the two must be independent: checking one must not quietly
# narrow by the other.


def test_the_location_box_matches_the_pin_badge(tmp_path):
    """The grid draws 📍 from has_gps and filters from the same predicate, so a
    tile can never show the badge yet be hidden by the box, or the reverse."""
    db_path = _archive(
        tmp_path,
        [
            (1, "sha1", None),
            (2, "sha2", None),
            (3, "sha3", None),
        ],
        located=[1, 3],
    )
    everything = queries.media(db_path, root_id=1)
    assert {i["id"]: i["has_gps"] for i in everything["items"]} == {1: True, 2: False, 3: True}
    with_loc = queries.media(db_path, root_id=1, located=True)
    without = queries.media(db_path, root_id=1, located=False)
    assert sorted(i["id"] for i in with_loc["items"]) == [1, 3]
    assert sorted(i["id"] for i in without["items"]) == [2]
    assert with_loc["total"] + without["total"] == everything["total"] == 3
    assert all(i["has_gps"] for i in with_loc["items"])


def test_the_two_boxes_are_independent(tmp_path):
    """Every combination of the two facts, so neither box can be found leaning
    on the other -- the bug a shared code path would most likely produce."""
    db_path = _archive(
        tmp_path,
        [
            (1, "sha1", ("sha1", CURRENT, "indexed")),  # indexed + located
            (2, "sha2", ("sha2", CURRENT, "indexed")),  # indexed only
            (3, "sha3", None),  # located only
            (4, "sha4", None),  # neither
        ],
        located=[1, 3],
    )

    def ids(**kw):
        return sorted(i["id"] for i in queries.media(db_path, root_id=1, **kw)["items"])

    assert ids(indexed=True, located=True) == [1]
    assert ids(indexed=True) == [1, 2]
    assert ids(located=True) == [1, 3]
    assert ids(indexed=False, located=False) == [4]


def test_the_box_offers_itself_once_anything_has_a_location(tmp_path):
    db_path = _archive(tmp_path, [(1, "sha1", None), (2, "sha2", None)])
    assert queries.browse_filters(db_path, 1)["located_any"] is False
    second = tmp_path / "b"
    second.mkdir()
    db_path = _archive(second, [(1, "sha1", None)], located=[1])
    assert queries.browse_filters(db_path, 1)["located_any"] is True


def test_a_description_search_can_be_narrowed_by_location(tmp_path):
    """The one place the two boxes behave differently: this filter stays live
    during a search, because unlike "indexed" it can still change the answer."""
    db_path = _archive(
        tmp_path,
        [
            (1, "sha1", ("sha1", CURRENT, "indexed")),
            (2, "sha2", ("sha2", CURRENT, "indexed")),
        ],
        located=[2],
    )
    query = [1.0, 0.0]
    everything = queries.semantic_search(db_path, query, root_id=1)
    assert sorted(i["id"] for i in everything["items"]) == [1, 2]
    narrowed = queries.semantic_search(db_path, query, root_id=1, located=True)
    assert [i["id"] for i in narrowed["items"]] == [2]
    assert narrowed["total"] == 1
