"""The Duplicates screen's two archive-wide numbers: "Unique files" and the
pending count under it.

Both describe the archive rather than the groups, and both have to keep
saying something sensible on an archive dedup has never run on -- which is
exactly when the screen used to replace itself with an empty-state box and
show no numbers at all.
"""

from organize_archive.db import database as db
from organize_archive.services import dups


def _catalog(tmp_path, files, covered=None):
    """A catalog with `files` as (id, hidden) pairs, optionally carrying a
    dedup_runs marker recording that `covered` files were grouped."""
    db_path = tmp_path / "archive.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/photos','2026-01-01')")
    for file_id, hidden in files:
        conn.execute(
            """INSERT INTO files(id,root_id,rel_path,ext,size,mtime,media_type,
                                 sha256,first_seen,last_seen,hidden)
               VALUES(?,1,?,'jpg',10,0,'image',?,'2026-01-01','2026-01-01',?)""",
            (file_id, f"{file_id}.jpg", str(file_id) * 64, hidden),
        )
    if covered is not None:
        db.dedup_mark_done(conn, 1, covered, max(f for f, _ in files))
    conn.commit()
    conn.close()
    return str(db_path)


def test_unique_counts_each_duplicate_group_once(tmp_path):
    # Five files, of which two are non-canonical copies dedup has hidden.
    path = _catalog(tmp_path, [(1, 0), (2, 1), (3, 0), (4, 1), (5, 0)], covered=5)

    result = dups.dup_summary(path, root_id=1)

    assert result["unique"] == 3


def test_unique_counts_every_file_before_dedup_has_run(tmp_path):
    """Nothing is hidden yet, so every file is still its own unique one --
    the tile reads as the archive's size rather than as zero."""
    path = _catalog(tmp_path, [(1, 0), (2, 0), (3, 0)])

    result = dups.dup_summary(path, root_id=1)

    assert (result["unique"], result["groups"], result["duplicates"]) == (3, 0, 0)


def test_pending_covers_the_whole_archive_until_a_run_lands(tmp_path):
    path = _catalog(tmp_path, [(1, 0), (2, 0), (3, 0)])

    assert dups.dup_summary(path, root_id=1)["pending"] == 3


def test_pending_is_what_the_last_run_did_not_cover(tmp_path):
    # A run covered three files; two more have been scanned since.
    path = _catalog(tmp_path, [(1, 0), (2, 0), (3, 0), (4, 0), (5, 0)], covered=3)

    assert dups.dup_summary(path, root_id=1)["pending"] == 2


def test_nothing_pending_once_the_run_covers_everything(tmp_path):
    path = _catalog(tmp_path, [(1, 0), (2, 1), (3, 0)], covered=3)

    assert dups.dup_summary(path, root_id=1)["pending"] == 0


def test_a_marker_ahead_of_the_catalog_never_reports_negative_pending(tmp_path):
    """Files removed since the last run leave the marker counting more than
    the archive now holds; that is not work owed."""
    path = _catalog(tmp_path, [(1, 0), (2, 0)], covered=9)

    assert dups.dup_summary(path, root_id=1)["pending"] == 0
