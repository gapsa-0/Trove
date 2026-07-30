from organize_archive.db import database as db
from organize_archive.web import queries


def _catalogue_with_people(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/photos','2026-01-01')")
    for file_id in (1, 2, 3):
        conn.execute(
            """INSERT INTO files(
                   id,root_id,rel_path,size,mtime,media_type,first_seen,last_seen
               ) VALUES(?,1,?,1,0,'image','2026-01-01','2026-01-01')""",
            (file_id, f"{file_id}.jpg"),
        )
        conn.execute(
            "INSERT INTO dates(file_id,best_datetime) VALUES(?,'2026-01-01')",
            (file_id,),
        )
    conn.executemany(
        "INSERT INTO persons(id,name,created_at) VALUES(?,?,'2026-01-01')",
        ((1, "Alice"), (2, "Bob")),
    )
    # Alice is in files 1 and 3; Bob is in files 2 and 3.
    for face_id, file_id, person_id in ((1, 1, 1), (2, 3, 1), (3, 2, 2), (4, 3, 2)):
        conn.execute(
            """INSERT INTO faces(
                   id,file_id,box_x,box_y,box_w,box_h,embedding,person_id,created_at
               ) VALUES(?,?,0,0,1,1,X'00',?,'2026-01-01')""",
            (face_id, file_id, person_id),
        )
    conn.commit()
    conn.close()
    return db_path


def test_media_multiple_people_requires_every_selected_person(tmp_path):
    db_path = _catalogue_with_people(tmp_path)

    result = queries.media(db_path, root_id=1, person_ids=[1, 2])

    assert [item["id"] for item in result["items"]] == [3]
    assert result["total"] == 1


def test_media_total_is_full_filtered_count_not_page_size(tmp_path):
    db_path = _catalogue_with_people(tmp_path)

    unfiltered = queries.media(db_path, root_id=1, limit=1, offset=0)
    result = queries.media(db_path, root_id=1, person_ids=[1], limit=1, offset=0)

    assert unfiltered["total"] == 3
    assert len(result["items"]) == 1
    assert result["count"] == 1
    assert result["total"] == 2


def test_timeline_multiple_people_uses_same_joined_filter(tmp_path):
    db_path = _catalogue_with_people(tmp_path)

    result = queries.timeline(db_path, root_id=1, person_ids=[1, 2])

    assert result["series"] == [{"period": "2026-01", "total": 1, "image": 1}]
