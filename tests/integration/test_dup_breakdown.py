"""The Duplicates page's split of what the redundant copies are (#35).

Byte-identical to the copy that was kept, or merely visually the same -- which
is what decides whether the space is safe to reclaim, and it has to reconcile
exactly with the count sitting right above it on the page.

The case that makes this non-trivial: a *perceptual* group routinely also
contains byte-identical copies, so the split has to be decided per member from
its sha256, not from the group's `method`. That is also the rule the duplicate
tiles label themselves with (dups.dup_groups' match_type), so the line under the
tile can never contradict the tiles below it.

Two further breakdowns, both by media type, were dropped when the panel of
stacked bars became this one line: nothing read them, and the unique-files one
cost a GROUP BY over every file in the archive on every poll to say what the
Overview's storage panel already says.
"""

from trove.db import database as db
from trove.services import dups


def _catalog_with_duplicates(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/photos','2026-01-01')")

    def add_file(file_id, sha, size, media_type="image"):
        conn.execute(
            """INSERT INTO files(id,root_id,rel_path,ext,size,mtime,media_type,
                                 sha256,first_seen,last_seen)
               VALUES(?,1,?,'jpg',?,0,?,?,'2026-01-01','2026-01-01')""",
            (file_id, f"{file_id}.jpg", size, media_type, sha),
        )

    def add_group(gid, method, canonical, duplicates):
        redundant = sum(size for _, size in duplicates)
        conn.execute(
            """INSERT INTO dup_groups(id,method,canonical_file_id,member_count,
                                      size_each,redundant_bytes,created_at)
               VALUES(?,?,?,?,?,?,'2026-01-01')""",
            (gid, method, canonical, 1 + len(duplicates), None, redundant),
        )
        conn.execute(
            "INSERT INTO dup_members(group_id,file_id,role) VALUES(?,?,'canonical')",
            (gid, canonical),
        )
        for file_id, _ in duplicates:
            conn.execute(
                "INSERT INTO dup_members(group_id,file_id,role) VALUES(?,?,'duplicate')",
                (gid, file_id),
            )

    # Group 1: a plain exact pair.
    add_file(1, "a" * 64, 100)
    add_file(2, "a" * 64, 100)
    add_group(1, "exact", 1, [(2, 100)])

    # Group 2: a perceptual group that ALSO holds a byte-identical copy --
    # file 5 matches the canonical's sha, file 4 only looks the same.
    add_file(3, "b" * 64, 300)
    add_file(4, "c" * 64, 120)
    add_file(5, "b" * 64, 300)
    add_group(2, "perceptual", 3, [(4, 120), (5, 300)])

    # Group 3: videos -- few copies, most of the bytes.
    add_file(6, "d" * 64, 1000, media_type="video")
    add_file(7, "d" * 64, 1000, media_type="video")
    add_group(3, "exact", 6, [(7, 1000)])

    conn.commit()
    conn.close()
    return db_path


def test_the_split_reconciles_with_the_headline_numbers(tmp_path):
    result = dups.dup_summary(str(_catalog_with_duplicates(tmp_path)), root_id=1)

    assert (result["groups"], result["duplicates"]) == (3, 4)
    assert sum(i["count"] for i in result["by_match"]) == result["duplicates"]
    assert sum(i["bytes"] for i in result["by_match"]) == result["reclaimable"]


def test_identical_copies_inside_a_perceptual_group_count_as_identical(tmp_path):
    result = dups.dup_summary(str(_catalog_with_duplicates(tmp_path)), root_id=1)

    by_match = {i["key"]: i for i in result["by_match"]}
    # Files 2, 5 and 7 share their canonical's sha256; only file 4 does not.
    assert by_match["identical"]["count"] == 3
    assert by_match["identical"]["bytes"] == 100 + 300 + 1000
    assert by_match["visual"]["count"] == 1
    assert by_match["visual"]["bytes"] == 120
    # Identical first: the fixed order is what keeps the bar's colours stable.
    assert [i["key"] for i in result["by_match"]] == ["identical", "visual"]


def test_an_archive_with_no_duplicates_reports_an_empty_split(tmp_path):
    db_path = tmp_path / "empty.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/photos','2026-01-01')")
    conn.commit()
    conn.close()

    result = dups.dup_summary(str(db_path), root_id=1)

    assert result["duplicates"] == 0
    # Nothing to split, so the tile says its number and nothing under it.
    assert result["by_match"] == []
