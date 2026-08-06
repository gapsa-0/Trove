"""The detail panel's payload: what it can say about one file, and -- the part
that is easy to get wrong -- what it must refuse to claim.

"Nothing found" and "not looked at yet" are different facts. Every stage writes
one row per file the moment it looks, including for files it skipped, so a
missing row already means "not yet". These tests pin that reading, because the
panel's wording depends on it and a regression here is invisible: the panel
would simply start saying a file has no faces when nothing has looked for one.
"""

from trove.db import database as db
from trove.services import item_detail

FIXED = "2026-01-01T00:00:00"


def _catalog(tmp_path):
    conn = db.connect(tmp_path / "archive.db")
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/photos',?)", (FIXED,))
    for fid, rel, mtype, ext in (
        (1, "trip/lake.jpg", "image", "jpg"),
        (2, "trip/letter.jpg", "image", "jpg"),
        (3, "papers/contract.pdf", "document", "pdf"),
    ):
        conn.execute(
            """INSERT INTO files(id,root_id,rel_path,ext,size,mtime,media_type,
                                 sha256,first_seen,last_seen)
               VALUES(?,1,?,?,1024,0,?,?,?,?)""",
            (fid, rel, ext, mtype, f"sha{fid}", FIXED, FIXED),
        )
    conn.commit()
    return conn


def test_a_file_no_stage_has_reached_reports_every_stage_as_unread(tmp_path):
    conn = _catalog(tmp_path)
    conn.close()

    it = item_detail.item(str(tmp_path / "archive.db"), 1)

    # No scan rows at all: nothing has looked, and the panel must not be able
    # to render this as "read, and no faces here".
    assert it["read"] == {
        "people": False,
        "pets": False,
        "text": False,
        "semantic": False,
        "duplicates": False,
    }


def test_a_stage_that_found_nothing_still_counts_as_read(tmp_path):
    conn = _catalog(tmp_path)
    # face_scan gets a row with n_faces=0: looked, found none. This is exactly
    # the case that must not be confused with "not looked at yet".
    conn.execute("INSERT INTO face_scan(file_id,n_faces,scanned_at) VALUES(1,0,?)", (FIXED,))
    conn.execute(
        "INSERT INTO pet_scan(file_id,n_animals,model_source,scanned_at) VALUES(1,0,'m',?)",
        (FIXED,),
    )
    conn.commit()
    conn.close()

    it = item_detail.item(str(tmp_path / "archive.db"), 1)

    assert it["read"]["people"] is True
    assert it["read"]["pets"] is True
    assert it["people"] == []  # and it genuinely found nothing
    assert it["read"]["semantic"] is False  # untouched by the above


def test_a_skipped_file_counts_as_read_so_it_stops_looking_pending(tmp_path):
    conn = _catalog(tmp_path)
    # doc_text writes a row for skips and errors too -- that is what makes the
    # pass resumable -- so a skip is "read", not "still queued".
    conn.execute(
        """INSERT INTO doc_text(file_id,source_sha256,wanted,extractor,status,
                                chars,n_chunks,extracted_at)
           VALUES(3,'sha3','documents','pdf-text','skipped',0,0,?)""",
        (FIXED,),
    )
    conn.commit()
    conn.close()

    it = item_detail.item(str(tmp_path / "archive.db"), 3)

    assert it["read"]["text"] is True
    assert it["text"] is None  # nothing extracted, so nothing to report


def test_a_picture_reports_its_whole_transcript(tmp_path):
    conn = _catalog(tmp_path)
    conn.execute(
        """INSERT INTO doc_text(file_id,source_sha256,wanted,extractor,status,
                                confidence,chars,n_chunks,extracted_at)
           VALUES(2,'sha2','ocr','ocr','extracted',0.91,42,2,?)""",
        (FIXED,),
    )
    for ordinal, body in ((0, "Querida Elena:"), (1, "Un beso grande.")):
        cur = conn.execute(
            """INSERT INTO doc_chunks(file_id,ordinal,chars) VALUES(2,?,?)""",
            (ordinal, len(body)),
        )
        conn.execute("INSERT INTO doc_chunk_fts(rowid,text) VALUES(?,?)", (cur.lastrowid, body))
    conn.commit()
    conn.close()

    it = item_detail.item(str(tmp_path / "archive.db"), 2)

    assert it["text"]["reader"] == "ocr"
    # Whole transcript, in chunk order, not a snippet: for a picture the point
    # is what it says.
    assert it["text"]["transcript"] == "Querida Elena:\n\nUn beso grande."
    assert it["text"]["confidence"] == 0.91


def test_a_document_reports_its_reader_and_size_but_never_its_text(tmp_path):
    conn = _catalog(tmp_path)
    conn.execute(
        """INSERT INTO doc_text(file_id,source_sha256,wanted,extractor,status,
                                chars,pages,n_chunks,extracted_at)
           VALUES(3,'sha3','documents','pdf-text','extracted',24060,14,30,?)""",
        (FIXED,),
    )
    cur = conn.execute("INSERT INTO doc_chunks(file_id,ordinal,chars) VALUES(3,0,9)")
    conn.execute("INSERT INTO doc_chunk_fts(rowid,text) VALUES(?,?)", (cur.lastrowid, "CONTRATO"))
    conn.commit()
    conn.close()

    it = item_detail.item(str(tmp_path / "archive.db"), 3)

    assert it["text"]["reader"] == "documents"
    assert it["text"]["pages"] == 14
    assert it["text"]["chars"] == 24060
    # The document itself is what you read. Thousands of words in a side panel
    # is not a feature, and shipping them on every open is not free.
    assert "transcript" not in it["text"]


def test_the_folder_count_excludes_this_file_and_other_folders(tmp_path):
    conn = _catalog(tmp_path)
    conn.commit()
    conn.close()

    it = item_detail.item(str(tmp_path / "archive.db"), 1)

    assert it["folder"] == "trip"
    assert it["folder_count"] == 1  # letter.jpg, not contract.pdf, not itself


def test_the_folder_count_does_not_count_subfolders_or_a_sibling_prefix(tmp_path):
    conn = _catalog(tmp_path)
    for fid, rel in (
        (4, "trip/day2/beach.jpg"),  # a subfolder: under the prefix, not in the folder
        (5, "trip2/other.jpg"),  # a sibling whose name starts with the folder's
        (6, "trip/boat.jpg"),  # the only genuine addition
    ):
        conn.execute(
            """INSERT INTO files(id,root_id,rel_path,ext,size,mtime,media_type,
                                 sha256,first_seen,last_seen)
               VALUES(?,1,?,'jpg',1,0,'image',?,?,?)""",
            (fid, rel, f"sha{fid}", FIXED, FIXED),
        )
    conn.commit()
    conn.close()

    assert item_detail.item(str(tmp_path / "archive.db"), 1)["folder_count"] == 2


def test_a_file_at_the_root_counts_only_other_root_level_files(tmp_path):
    conn = _catalog(tmp_path)
    for fid, rel in ((4, "loose.jpg"), (5, "alone.jpg"), (6, "sub/deep.jpg")):
        conn.execute(
            """INSERT INTO files(id,root_id,rel_path,ext,size,mtime,media_type,
                                 sha256,first_seen,last_seen)
               VALUES(?,1,?,'jpg',1,0,'image',?,?,?)""",
            (fid, rel, f"sha{fid}", FIXED, FIXED),
        )
    conn.commit()
    conn.close()

    it = item_detail.item(str(tmp_path / "archive.db"), 4)

    assert it["folder"] == ""
    assert it["folder_count"] == 1  # alone.jpg only


def _grouped(conn, *, method="exact", member_count=3):
    """Files 1 and 2 in one group, 1 kept. Their hashes differ, so 2 is a
    visual match rather than a byte-identical copy."""
    conn.execute(
        """INSERT INTO dup_groups(id,method,canonical_file_id,member_count,created_at)
           VALUES(7,?,1,?,?)""",
        (method, member_count, FIXED),
    )
    conn.execute("INSERT INTO dup_members(group_id,file_id,role) VALUES(7,1,'canonical')")
    conn.execute("INSERT INTO dup_members(group_id,file_id,role) VALUES(7,2,'duplicate')")


def test_a_duplicate_group_is_reported_with_the_files_role(tmp_path):
    conn = _catalog(tmp_path)
    _grouped(conn)
    conn.commit()
    conn.close()

    db_path = str(tmp_path / "archive.db")
    kept = item_detail.item(db_path, 1)["duplicates"]
    assert (kept["group_id"], kept["method"], kept["count"], kept["canonical"]) == (
        7,
        "exact",
        3,
        True,
    )
    assert item_detail.item(db_path, 2)["duplicates"]["canonical"] is False
    assert item_detail.item(db_path, 3)["duplicates"] is None


def test_the_group_carries_its_copies_so_the_panel_can_show_them(tmp_path):
    """The panel draws the group, not a count of it: "3 copies" says three files
    somewhere are the same and leaves you to go and find them."""
    conn = _catalog(tmp_path)
    _grouped(conn)
    conn.commit()
    conn.close()

    members = item_detail.item(str(tmp_path / "archive.db"), 2)["duplicates"]["members"]

    # The kept copy first, whichever file you opened -- it is the one the group
    # is measured against.
    assert [(m["id"], m["role"], m["name"]) for m in members] == [
        (1, "canonical", "lake.jpg"),
        (2, "duplicate", "letter.jpg"),
    ]


def test_a_copy_is_identical_or_visual_by_its_own_bytes(tmp_path):
    """The same per-copy rule the Duplicates screen labels its tiles with: a
    perceptual group routinely holds byte-identical copies too, and deciding
    from the group's method would label the same file differently on the two
    screens."""
    conn = _catalog(tmp_path)
    _grouped(conn, method="phash")
    conn.execute("UPDATE files SET sha256='sha1' WHERE id=2")  # same bytes as the kept copy
    conn.commit()
    conn.close()

    members = item_detail.item(str(tmp_path / "archive.db"), 1)["duplicates"]["members"]

    assert [m["match_type"] for m in members] == ["canonical", "identical"]


def test_a_file_the_last_grouping_run_never_reached_is_not_yet_compared(tmp_path):
    """Dedup writes no per-file row -- a file with no copies is simply in no
    group -- so "no duplicates found" would otherwise be claimed for a file
    scanned after the last run, which nothing has compared against anything."""
    conn = _catalog(tmp_path)
    db.dedup_mark_done(conn, root_id=1, covered_files=2, covered_max_file_id=2)
    conn.commit()
    conn.close()

    db_path = str(tmp_path / "archive.db")
    assert item_detail.item(db_path, 2)["read"]["duplicates"] is True
    assert item_detail.item(db_path, 3)["read"]["duplicates"] is False


def test_an_unhashed_file_is_not_yet_compared_either(tmp_path):
    """`dedup/exact.py` groups on sha256, so a file the hashing pass has not
    reached is not eligible however far the run's mark reaches."""
    conn = _catalog(tmp_path)
    conn.execute("UPDATE files SET sha256=NULL WHERE id=1")
    db.dedup_mark_done(conn, root_id=1, covered_files=3, covered_max_file_id=3)
    conn.commit()
    conn.close()

    assert item_detail.item(str(tmp_path / "archive.db"), 1)["read"]["duplicates"] is False
