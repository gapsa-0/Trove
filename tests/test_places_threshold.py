"""place_min_media is a READ-time floor (config.py), never a clustering-time
one: place_clusters rows are never deleted for falling short, they just stop
being *reported*, except when named or pinned (see queries._PLACE_EXEMPT)."""

from organize_archive.db import database as db
from organize_archive.gui import queries


def _catalog_with_places(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/photos','2026-01-01')")
    for file_id in range(1, 8):
        conn.execute(
            """INSERT INTO files(id,root_id,rel_path,size,mtime,media_type,
                                 first_seen,last_seen)
               VALUES(?,1,?,1,0,'image','2026-01-01','2026-01-01')""",
            (file_id, f"{file_id}.jpg"))

    # Cluster 1: unnamed, unpinned, BELOW threshold (3 members) -> hidden.
    conn.execute(
        """INSERT INTO place_clusters(id,root_id,name,lat,lon,member_count,
                                      pinned,created_at)
           VALUES(1,1,NULL,-34.6,-58.4,3,0,'2026-01-01')""")
    for fid in (1, 2, 3):
        conn.execute(
            "INSERT INTO place_cluster_members(cluster_id,file_id,source) "
            "VALUES(1,?,'auto')", (fid,))

    # Cluster 2: NAMED, below threshold (2 members) -> exempt, still returned.
    conn.execute(
        """INSERT INTO place_clusters(id,root_id,name,lat,lon,member_count,
                                      pinned,created_at)
           VALUES(2,1,'Home',-34.6,-58.4,2,0,'2026-01-01')""")
    for fid in (4, 5):
        conn.execute(
            "INSERT INTO place_cluster_members(cluster_id,file_id,source) "
            "VALUES(2,?,'auto')", (fid,))

    # Cluster 3: PINNED (just created via create_place, 0 members) -> exempt.
    conn.execute(
        """INSERT INTO place_clusters(id,root_id,name,lat,lon,member_count,
                                      pinned,created_at)
           VALUES(3,1,NULL,-34.6,-58.4,0,1,'2026-01-01')""")

    # Cluster 4: unnamed, unpinned, ABOVE threshold -> returned on its own merits.
    conn.execute(
        """INSERT INTO place_clusters(id,root_id,name,lat,lon,member_count,
                                      pinned,created_at)
           VALUES(4,1,NULL,-34.6,-58.4,12,0,'2026-01-01')""")
    for fid in (6, 7):
        conn.execute(
            "INSERT INTO place_cluster_members(cluster_id,file_id,source) "
            "VALUES(4,?,'auto')", (fid,))

    conn.commit()
    conn.close()
    return db_path


def test_below_threshold_cluster_is_hidden_but_named_and_pinned_are_not(tmp_path):
    db_path = _catalog_with_places(tmp_path)

    result = queries.place_clusters(db_path, root_id=1, min_media=10)

    ids = {c["id"] for c in result["clusters"]}
    assert ids == {2, 3, 4}   # cluster 1 (unnamed, below threshold) is excluded
    assert result["hidden"] == {"places": 1, "files": 3}


def test_item_reports_no_place_for_a_hidden_membership(tmp_path):
    db_path = _catalog_with_places(tmp_path)

    # File 1's only place is cluster 1: unnamed, unpinned, below threshold.
    it = queries.item(str(db_path), 1, min_media=10)

    assert it["place"] is None


def test_item_still_reports_a_named_place_below_threshold(tmp_path):
    db_path = _catalog_with_places(tmp_path)

    # File 4's only place is cluster 2: named, below threshold -> exempt.
    it = queries.item(str(db_path), 4, min_media=10)

    assert it["place"] == {"id": 2, "name": "Home"}
