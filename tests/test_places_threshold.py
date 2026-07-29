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
            (file_id, f"{file_id}.jpg"),
        )

    # Cluster 1: unnamed, unpinned, BELOW threshold (3 members) -> hidden.
    conn.execute(
        """INSERT INTO place_clusters(id,root_id,name,lat,lon,member_count,
                                      pinned,created_at)
           VALUES(1,1,NULL,-34.6,-58.4,3,0,'2026-01-01')"""
    )
    for fid in (1, 2, 3):
        conn.execute(
            "INSERT INTO place_cluster_members(cluster_id,file_id,source) VALUES(1,?,'auto')",
            (fid,),
        )

    # Cluster 2: NAMED, below threshold (2 members) -> exempt, still returned.
    conn.execute(
        """INSERT INTO place_clusters(id,root_id,name,lat,lon,member_count,
                                      pinned,created_at)
           VALUES(2,1,'Home',-34.6,-58.4,2,0,'2026-01-01')"""
    )
    for fid in (4, 5):
        conn.execute(
            "INSERT INTO place_cluster_members(cluster_id,file_id,source) VALUES(2,?,'auto')",
            (fid,),
        )

    # Cluster 3: PINNED (just created via create_place, 0 members) -> exempt.
    conn.execute(
        """INSERT INTO place_clusters(id,root_id,name,lat,lon,member_count,
                                      pinned,created_at)
           VALUES(3,1,NULL,-34.6,-58.4,0,1,'2026-01-01')"""
    )

    # Cluster 4: unnamed, unpinned, ABOVE threshold -> returned on its own merits.
    conn.execute(
        """INSERT INTO place_clusters(id,root_id,name,lat,lon,member_count,
                                      pinned,created_at)
           VALUES(4,1,NULL,-34.6,-58.4,12,0,'2026-01-01')"""
    )
    for fid in (6, 7):
        conn.execute(
            "INSERT INTO place_cluster_members(cluster_id,file_id,source) VALUES(4,?,'auto')",
            (fid,),
        )

    # File 8 is geotagged but belongs to no cluster at all -- the stray the
    # un-clustered map view must still show, in grey.
    conn.execute(
        """INSERT INTO files(id,root_id,rel_path,size,mtime,media_type,
                             first_seen,last_seen)
           VALUES(8,1,'8.jpg',1,0,'image','2026-01-01','2026-01-01')"""
    )
    # Distinct latitudes so a returned point can be traced back to its file.
    for fid in range(1, 9):
        conn.execute(
            "INSERT INTO geo(file_id,lat,lon,alt,geo_source) VALUES(?,?,?,NULL,'exif')",
            (fid, -34.6 - fid / 1000, -58.4),
        )

    conn.commit()
    conn.close()
    return db_path


def test_below_threshold_cluster_is_hidden_but_named_and_pinned_are_not(tmp_path):
    db_path = _catalog_with_places(tmp_path)

    result = queries.place_clusters(db_path, root_id=1, min_media=10)

    ids = {c["id"] for c in result["clusters"]}
    assert ids == {2, 3, 4}  # cluster 1 (unnamed, below threshold) is excluded
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


# ---------------------------------------------------------------------------
# The un-clustered map view (things_to_fix #33) reads the same floor, but must
# still show the files it hides places for -- one dot per geotagged file, and
# the ones with no *shown* place identified as such so they can be greyed out.
# ---------------------------------------------------------------------------


def test_place_points_returns_every_geotagged_file(tmp_path):
    db_path = _catalog_with_places(tmp_path)

    result = queries.place_points(str(db_path), root_id=1, min_media=10)

    assert len(result["points"]) == 8  # every file with a geo row
    assert all(len(p) == 4 for p in result["points"])


def test_place_points_flags_files_with_no_shown_place(tmp_path):
    db_path = _catalog_with_places(tmp_path)

    result = queries.place_points(str(db_path), root_id=1, min_media=10)
    by_file = {p[3]: p[2] for p in result["points"]}

    # Files 1-3 sit in the below-threshold, unnamed cluster 1, and file 8 is in
    # no cluster: all four have no place to be coloured by.
    assert by_file[1] == by_file[2] == by_file[3] == 0
    assert by_file[8] == 0
    assert result["unplaced"] == 4
    # The exempt (named / pinned) and above-threshold clusters keep their ids,
    # so a point is coloured by exactly the place the map shows.
    assert by_file[4] == by_file[5] == 2
    assert by_file[6] == by_file[7] == 4


def test_place_points_ignores_files_from_another_root(tmp_path):
    db_path = _catalog_with_places(tmp_path)
    conn = db.connect(db_path)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(2,'/other','2026-01-01')")
    conn.execute(
        """INSERT INTO files(id,root_id,rel_path,size,mtime,media_type,
                             first_seen,last_seen)
           VALUES(99,2,'99.jpg',1,0,'image','2026-01-01','2026-01-01')"""
    )
    conn.execute("INSERT INTO geo(file_id,lat,lon,alt,geo_source) VALUES(99,10.0,10.0,NULL,'exif')")
    conn.commit()
    conn.close()

    result = queries.place_points(str(db_path), root_id=1, min_media=10)

    assert 99 not in {p[3] for p in result["points"]}
