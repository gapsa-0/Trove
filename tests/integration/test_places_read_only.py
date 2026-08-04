"""Reading the map must not write to the database.

``place_clusters()`` used to cluster a root on first call if it had no rows
yet. That put a write behind a GET, with two consequences: it could raise
"database is locked" while the pipeline held the writer (GET routes are
correctly not wrapped in ``write_with_retry``), and any page on any website
could trigger it with an ``<img src=".../api/map/clusters?root=1">``. The
clustering belongs to the places stage, which already bootstraps a root that
has none -- these tests pin both halves of that split.
"""

from __future__ import annotations

import logging
import threading

from trove.config import Config
from trove.db import database as db
from trove.pipeline.job import Job, JobContext
from trove.pipeline.runners import places as places_runner
from trove.services import places


def _geotagged_catalog(tmp_path):
    """A root with geotagged files and no place_clusters rows: exactly the
    state a freshly scanned archive is in before the places stage runs."""
    db_path = tmp_path / "archive.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/photos','2026-01-01')")
    for file_id in range(1, 13):
        conn.execute(
            """INSERT INTO files(id,root_id,rel_path,size,mtime,media_type,
                                 first_seen,last_seen)
               VALUES(?,1,?,1,0,'image','2026-01-01','2026-01-01')""",
            (file_id, f"{file_id}.jpg"),
        )
        # All within a few metres of each other, so they cluster as one place.
        conn.execute(
            "INSERT INTO geo(file_id,lat,lon,alt,geo_source) VALUES(?,?,?,NULL,'exif')",
            (file_id, -34.6 + file_id / 100000, -58.4),
        )
    conn.commit()
    conn.close()
    return db_path


def _cluster_count(db_path) -> int:
    conn = db.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM place_clusters").fetchone()[0]
    finally:
        conn.close()


def test_reading_clusters_on_an_unprocessed_root_writes_nothing(tmp_path):
    db_path = _geotagged_catalog(tmp_path)

    result = places.place_clusters(db_path, root_id=1, min_media=10)

    assert result["clusters"] == []
    assert _cluster_count(db_path) == 0, "a read created place_clusters rows"


def test_reading_points_on_an_unprocessed_root_still_shows_the_files(tmp_path):
    """The map is not blank meanwhile -- every geotagged file is still a dot,
    just an unplaced one, which is what the un-clustered view already draws."""
    db_path = _geotagged_catalog(tmp_path)

    result = places.place_points(str(db_path), root_id=1, min_media=10)

    assert len(result["points"]) == 12
    assert result["unplaced"] == 12


def test_the_places_stage_is_what_bootstraps_a_root(tmp_path):
    db_path = _geotagged_catalog(tmp_path)
    conn = db.connect(db_path)
    job = Job(id=1, kind="places", root_id=1, root_path="/photos")
    ctx = JobContext(
        cfg=Config(), job=job, cancel=threading.Event(), conn=conn, log=logging.getLogger(__name__)
    )
    try:
        places_runner.run(ctx)
        conn.commit()
    finally:
        conn.close()

    assert _cluster_count(db_path) == 1
    assert [c["id"] for c in places.place_clusters(db_path, root_id=1, min_media=10)["clusters"]]
