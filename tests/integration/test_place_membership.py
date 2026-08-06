"""What can be a member of a place.

Only something that was taken can have been taken somewhere. The automatic
clustering never had to be told -- it works from EXIF coordinates, which only a
camera writes -- so this is about the two calls that attach a file by hand, where
the file is whatever the user happened to have open.
"""

from trove.db import database as db
from trove.services import places_edit


def _catalog(tmp_path):
    """One archive, one photo, one PDF, and a place to attach them to."""
    db_path = tmp_path / "archive.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/photos','2026-01-01')")
    for file_id, rel_path, media_type in (
        (1, "beach.jpg", "image"),
        (2, "clip.mp4", "video"),
        (3, "lease.pdf", "document"),
    ):
        conn.execute(
            """INSERT INTO files(id,root_id,rel_path,size,mtime,media_type,
                                 first_seen,last_seen)
               VALUES(?,1,?,1,0,?,'2026-01-01','2026-01-01')""",
            (file_id, rel_path, media_type),
        )
    conn.execute(
        """INSERT INTO place_clusters(id,root_id,name,lat,lon,member_count,
                                      pinned,created_at)
           VALUES(1,1,'Home',-34.6,-58.4,0,1,'2026-01-01')"""
    )
    conn.commit()
    conn.close()
    return db_path


def _members(db_path):
    conn = db.open_readonly(db_path)
    try:
        return [r["file_id"] for r in conn.execute("SELECT file_id FROM place_cluster_members")]
    finally:
        conn.close()


def test_a_photo_or_a_video_can_be_put_on_the_map_by_hand(tmp_path):
    db_path = _catalog(tmp_path)

    assert places_edit.set_place(db_path, 1, 1).get("ok")
    assert places_edit.set_place(db_path, 2, 1).get("ok")

    assert _members(db_path) == [1, 2]


def test_a_document_cannot_be_somewhere(tmp_path):
    """A spreadsheet was written, not taken; where the laptop was that day is
    not a fact about the file. The panel stopped offering it, and this is the
    same rule kept where a second caller cannot drift back past it."""
    db_path = _catalog(tmp_path)

    answer = places_edit.set_place(db_path, 3, 1)

    assert "photos and videos" in answer["error"]
    assert _members(db_path) == []


def test_a_place_pinned_for_a_document_is_not_created_at_all(tmp_path):
    """The file is the reason that call exists -- "put *this* on the map here"
    -- so refusing it half way would leave an empty pin behind as the record of
    a rejected request."""
    db_path = _catalog(tmp_path)

    answer = places_edit.create_place(db_path, 1, "Office", -34.6, -58.4, file_id=3)

    assert "photos and videos" in answer["error"]
    conn = db.open_readonly(db_path)
    try:
        assert [r["name"] for r in conn.execute("SELECT name FROM place_clusters")] == ["Home"]
    finally:
        conn.close()
