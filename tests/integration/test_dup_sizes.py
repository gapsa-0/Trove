"""What a duplicate group states about size: only what freeing it gives back.

`dup_groups.size_each` is the CANONICAL's size, and only describes every copy
when the group is exact -- byte-identical files are the same size by
definition. A perceptual group is routinely a big original beside smaller
re-compressed exports, and there the column was being read out as what all of
them weigh, which contradicted `redundant_bytes` printed beside it: a 218 KB
original with two 107 KB copies claimed "218.1 KB each" and "214.1 KB
reclaimable" in the same breath. The listing now states the reclaimable figure
alone, so there is no per-copy size in the payload to be read out wrongly
again.
"""

from trove.db import database as db
from trove.services import dups


def _catalog(tmp_path, sizes):
    """One perceptual group: `sizes[0]` is the canonical, the rest are copies."""
    db_path = tmp_path / "archive.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/photos','2026-01-01')")
    for file_id, size in enumerate(sizes, start=1):
        conn.execute(
            """INSERT INTO files(id,root_id,rel_path,ext,size,mtime,media_type,
                                 sha256,first_seen,last_seen)
               VALUES(?,1,?,'jpg',?,0,'image',?,'2026-01-01','2026-01-01')""",
            (file_id, f"{file_id}.jpg", size, str(file_id) * 64),
        )
    conn.execute(
        """INSERT INTO dup_groups(id,method,canonical_file_id,member_count,
                                  size_each,redundant_bytes,created_at)
           VALUES(1,'perceptual',1,?,?,?,'2026-01-01')""",
        (len(sizes), sizes[0], sum(sizes[1:])),
    )
    conn.executemany(
        "INSERT INTO dup_members(group_id,file_id,role) VALUES(1,?,?)",
        [(i, "canonical" if i == 1 else "duplicate") for i in range(1, len(sizes) + 1)],
    )
    # As a grouping run leaves it: `files.hidden` is what says which copies are
    # kept, and the listing reads it back.
    conn.executemany(
        "UPDATE files SET dup_group_id=1, hidden=? WHERE id=?",
        [(int(i != 1), i) for i in range(1, len(sizes) + 1)],
    )
    conn.commit()
    conn.close()
    return str(db_path)


def _group(path):
    return dups.dup_groups(path, root_id=1)["groups"][0]


def test_a_group_states_only_what_freeing_it_reclaims(tmp_path):
    """The case from the screen: one 218 KB original, two 107 KB exports."""
    g = _group(_catalog(tmp_path, [223_334, 109_670, 109_670]))

    assert g["reclaimable"] == 109_670 * 2
    assert "size_each" not in g


def test_a_member_carries_its_own_size_and_the_group_still_carries_none(tmp_path):
    """A copy may say what it weighs; the group may not say what its copies do.

    That is the distinction the header got wrong -- one number standing for
    every copy -- and it is not the same as refusing sizes altogether. Once the
    user can choose which copies are kept, what the group reclaims depends on
    which ones those are, and only the per-copy sizes can answer it.
    """
    g = _group(_catalog(tmp_path, [223_334, 109_670, 109_670]))

    assert "size_each" not in g
    assert sorted(m["size"] for m in g["members"]) == [109_670, 109_670, 223_334]
    # ...and they reconcile with the saving the group states, which is what
    # keeps the row's own arithmetic honest as copies are kept and dropped.
    assert sum(m["size"] for m in g["members"] if not m["kept"]) == g["reclaimable"]


def test_copies_of_one_size_are_described_no_differently(tmp_path):
    """An exact group is the case `size_each` was true for. It gets the same
    treatment: the saving is the whole claim, for every group."""
    g = _group(_catalog(tmp_path, [100_000, 100_000, 100_000]))

    assert g["reclaimable"] == 200_000
    assert "size_each" not in g
