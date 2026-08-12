"""What the "Merge with…" picker is allowed to offer.

One function answers for people, pets and places, which is the point: it is one
question asked of three tables, and services/merging.py is where that symmetry
already lives.
"""

from trove.db import database as db
from trove.services import merging


def _archive(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/photos','2026-01-01')")
    for pid, name in ((1, "Ana"), (2, None), (3, "  "), (4, "Bruno")):
        conn.execute(
            "INSERT INTO persons(id,name,created_at) VALUES(?,?,'2026-01-01')", (pid, name)
        )
    conn.execute(
        "INSERT INTO pets(id,name,species,created_at) VALUES(1,'Rocco','dog','2026-01-01')"
    )
    conn.execute("INSERT INTO pets(id,name,species,created_at) VALUES(2,NULL,'cat','2026-01-01')")
    conn.execute(
        """INSERT INTO place_clusters(id,root_id,name,lat,lon,member_count,created_at)
           VALUES(1,1,'Home',0,0,3,'2026-01-01')"""
    )
    conn.commit()
    conn.close()
    return db_path


def test_only_named_clusters_are_offered(tmp_path):
    """An unnamed group has nothing to identify it by in a list, and its id does
    not survive the next recluster -- so it is not a thing to merge *into*."""
    db_path = _archive(tmp_path)
    assert [t["name"] for t in merging.named_targets(db_path, "person")] == ["Ana", "Bruno"]


def test_a_whitespace_name_counts_as_unnamed(tmp_path):
    """Same rule the People grid sorts by, so the two cannot disagree about
    which groups are named."""
    db_path = _archive(tmp_path)
    assert all(t["name"].strip() for t in merging.named_targets(db_path, "person"))


def test_a_cluster_is_not_offered_itself(tmp_path):
    db_path = _archive(tmp_path)
    targets = merging.named_targets(db_path, "person", exclude_id=1)
    assert [t["id"] for t in targets] == [4]


def test_each_kind_reads_its_own_table(tmp_path):
    db_path = _archive(tmp_path)
    assert [t["name"] for t in merging.named_targets(db_path, "pet")] == ["Rocco"]
    assert [t["name"] for t in merging.named_targets(db_path, "place")] == ["Home"]


def test_an_unknown_kind_offers_nothing_rather_than_failing(tmp_path):
    """The entity arrives from a query string, so a typo must not be a 500."""
    db_path = _archive(tmp_path)
    assert merging.named_targets(db_path, "sqlite_master") == []
