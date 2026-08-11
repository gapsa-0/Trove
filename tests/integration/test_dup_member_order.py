"""The order the copies inside one group come back in.

The screen draws a group as a row of pictures that all look the same, so where
each one lives is the only thing telling them apart. In insertion order that row
reads as nothing -- the ids are scan order, which is whatever the disk walk
happened to reach first. In path order the copies sharing a folder sit together,
which is how someone clearing them out actually works through them.

The canonical stays first whatever else happens: it is the one the rest are
being compared against, and the screen tags it as the copy that is kept.
"""

from trove.db import database as db
from trove.services import dups

# Deliberately inserted in an order that is neither alphabetical nor the reverse
# of it, so a test that passes cannot be passing on insertion order by accident.
PATHS = [
    "Takeout/Google Photos/2021/shot.jpg",
    "Old drive/backup/pictures/shot.jpg",
    "Phone dump/DCIM/Camera/shot.jpg",
    "Old drive/backup/pictures/varios/shot.jpg",
]
CANONICAL = "Zzz last by path/shot.jpg"


def _catalog(tmp_path):
    """One group: a canonical that sorts last by path, and four copies."""
    db_path = tmp_path / "archive.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/photos','2026-01-01')")
    for file_id, rel_path in enumerate([CANONICAL, *PATHS], start=1):
        conn.execute(
            """INSERT INTO files(id,root_id,rel_path,ext,size,mtime,media_type,
                                 sha256,first_seen,last_seen)
               VALUES(?,1,?,'jpg',100,0,'image',?,'2026-01-01','2026-01-01')""",
            (file_id, rel_path, "a" * 64),
        )
    conn.execute(
        """INSERT INTO dup_groups(id,method,canonical_file_id,member_count,
                                  size_each,redundant_bytes,created_at)
           VALUES(1,'exact',1,5,100,400,'2026-01-01')"""
    )
    conn.executemany(
        "INSERT INTO dup_members(group_id,file_id,role) VALUES(1,?,?)",
        [(fid, "canonical" if fid == 1 else "duplicate") for fid in range(1, 6)],
    )
    conn.commit()
    conn.close()
    return str(db_path)


def _members(tmp_path):
    page = dups.dup_groups(_catalog(tmp_path), root_id=1)
    return page["groups"][0]["members"]


def test_the_kept_copy_comes_first_however_its_path_sorts(tmp_path):
    """Path order applies *within* the copies, not across the role boundary."""
    members = _members(tmp_path)

    assert members[0]["role"] == "canonical"
    assert members[0]["folder"] == "Zzz last by path"


def test_the_copies_are_in_path_order_not_scan_order(tmp_path):
    """The regression this exists for: they used to come back ordered by file
    id, which is the order the disk walk reached them in and means nothing to
    a reader comparing folders."""
    copies = [m for m in _members(tmp_path) if m["role"] == "duplicate"]

    assert [m["folder"] for m in copies] == [
        "Old drive/backup/pictures",
        "Old drive/backup/pictures/varios",
        "Phone dump/DCIM/Camera",
        "Takeout/Google Photos/2021",
    ]


def test_every_copy_carries_the_name_the_screen_shows(tmp_path):
    """The tiles are captioned with the file's own name -- the one thing that
    tells two copies of one picture apart. It has always been in the payload
    and went unused for long enough that it is worth a test saying it is not
    decoration."""
    assert all(m["name"] == "shot.jpg" for m in _members(tmp_path))
