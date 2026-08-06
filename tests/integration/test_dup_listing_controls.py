"""Filtering the duplicate listing by match type, and ordering it by group size.

The filter has to be decided per MEMBER from its sha256, not from the group's
own `method`: a perceptual group routinely also holds byte-identical copies, so
a filter reading `method` would return groups whose tiles are tagged the other
way and contradict the breakdown panel above them (see test_dup_breakdown).

Ordering matters for paging as much as for reading: member counts tie
constantly, so every ordering has to break ties the same way on every request
or groups are dropped and repeated at the page seam.
"""

from trove.db import database as db
from trove.services import dups


def _catalog(tmp_path):
    """Three groups: one purely identical, one purely visual, one holding both."""
    db_path = tmp_path / "archive.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/photos','2026-01-01')")
    next_id = iter(range(1, 100))

    def add_group(gid, canonical_sha, copies, redundant):
        """`copies` are the duplicates' sha256s -- equal to the canonical's for
        an identical copy, different for a visual match."""
        canon = next(next_id)
        members = [(canon, canonical_sha)] + [(next(next_id), s) for s in copies]
        for file_id, sha in members:
            conn.execute(
                """INSERT INTO files(id,root_id,rel_path,ext,size,mtime,media_type,
                                     sha256,first_seen,last_seen)
                   VALUES(?,1,?,'jpg',100,0,'image',?,'2026-01-01','2026-01-01')""",
                (file_id, f"{file_id}.jpg", sha),
            )
        # The group before its members: dup_members carries a foreign key onto it.
        conn.execute(
            """INSERT INTO dup_groups(id,method,canonical_file_id,member_count,
                                      size_each,redundant_bytes,created_at)
               VALUES(?,?,?,?,100,?,'2026-01-01')""",
            (
                gid,
                "exact" if all(s == canonical_sha for s in copies) else "perceptual",
                canon,
                len(members),
                redundant,
            ),
        )
        conn.executemany(
            "INSERT INTO dup_members(group_id,file_id,role) VALUES(?,?,?)",
            [(gid, fid, "canonical" if fid == canon else "duplicate") for fid, _ in members],
        )

    # Group 1: two byte-identical copies. Biggest saving, fewest members.
    add_group(1, "a" * 64, ["a" * 64, "a" * 64], redundant=900)
    # Group 2: one visual match only. Smallest saving.
    add_group(2, "b" * 64, ["c" * 64], redundant=100)
    # Group 3: a perceptual group that ALSO holds an identical copy, so it must
    # come back under BOTH filters. Most members.
    add_group(3, "d" * 64, ["d" * 64, "e" * 64, "f" * 64], redundant=300)
    conn.commit()
    conn.close()
    return str(db_path)


def _ids(page):
    return [g["id"] for g in page["groups"]]


def test_unfiltered_listing_is_unchanged(tmp_path):
    page = dups.dup_groups(_catalog(tmp_path), root_id=1)

    assert _ids(page) == [1, 3, 2]  # biggest reclaimable first
    assert page["total"] == 3


def test_only_groups_holding_an_identical_copy(tmp_path):
    page = dups.dup_groups(_catalog(tmp_path), root_id=1, match="identical")

    # Group 3 qualifies even though it is a *perceptual* group: one of its
    # copies is byte-identical to the kept file.
    assert _ids(page) == [1, 3]
    assert page["total"] == 2


def test_only_groups_holding_a_visual_match(tmp_path):
    page = dups.dup_groups(_catalog(tmp_path), root_id=1, match="visual")

    assert _ids(page) == [3, 2]
    assert page["total"] == 2


def test_sorting_by_how_many_copies_a_group_holds(tmp_path):
    path = _catalog(tmp_path)

    assert _ids(dups.dup_groups(path, root_id=1, sort="count_desc")) == [3, 1, 2]
    assert _ids(dups.dup_groups(path, root_id=1, sort="count_asc")) == [2, 1, 3]


def test_a_sort_and_a_filter_apply_together(tmp_path):
    page = dups.dup_groups(_catalog(tmp_path), root_id=1, match="visual", sort="count_asc")

    assert _ids(page) == [2, 3]
    assert page["total"] == 2


def test_paging_a_sorted_listing_never_drops_or_repeats_a_group(tmp_path):
    """Groups 1 and 2 both hold... nothing in common, but ties are the norm on
    member count, and a tie broken differently between two requests loses a
    group at the seam. Every ordering ends on the group id to stop that."""
    path = _catalog(tmp_path)

    seen = []
    for offset in (0, 1, 2):
        seen += _ids(dups.dup_groups(path, root_id=1, sort="count_desc", limit=1, offset=offset))

    assert seen == [3, 1, 2]


def test_an_unknown_filter_or_sort_falls_back_to_the_plain_listing(tmp_path):
    """A hand-edited URL must not reach the SQL, and must not narrow the list to
    something the screen has no control to undo."""
    path = _catalog(tmp_path)
    plain = _ids(dups.dup_groups(path, root_id=1))

    assert _ids(dups.dup_groups(path, root_id=1, match="'; DROP TABLE files--")) == plain
    assert _ids(dups.dup_groups(path, root_id=1, sort="g.id; DELETE FROM files")) == plain
    assert _ids(dups.dup_groups(path, root_id=1, match=None, sort=None)) == plain
