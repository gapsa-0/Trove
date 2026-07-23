from organize_archive.db import database as db
from organize_archive.gui.queries import remove_archive


def _file(conn, root_id, path, digest):
    return conn.execute(
        """INSERT INTO files(root_id, rel_path, size, mtime, media_type, sha256,
                             first_seen, last_seen)
           VALUES(?, ?, 1, 0, 'image', ?, 'now', 'now')""",
        (root_id, path, digest),
    ).lastrowid


def test_remove_archive_keeps_other_root_and_shared_thumbnail(tmp_path):
    db_path = tmp_path / "archive.db"
    cache = tmp_path / "cache"
    conn = db.connect(db_path)
    db.init_db(conn)
    first = db.get_or_create_root(conn, "/one")
    second = db.get_or_create_root(conn, "/two")
    first_file = _file(conn, first, "one.jpg", "shared")
    _file(conn, first, "only-one.jpg", "only-one")
    second_file = _file(conn, second, "two.jpg", "shared")
    now = db.now_iso()
    group = conn.execute(
        """INSERT INTO dup_groups(method, canonical_file_id, member_count, created_at)
           VALUES('exact', ?, 2, ?)""", (first_file, now),
    ).lastrowid
    conn.executemany(
        "INSERT INTO dup_members(group_id, file_id, role) VALUES(?, ?, ?)",
        [(group, first_file, "canonical"), (group, second_file, "duplicate")],
    )
    conn.execute("UPDATE files SET hidden=1, dup_group_id=? WHERE id=?",
                 (group, second_file))
    conn.commit()
    conn.close()

    thumbs = cache / "thumbs"
    thumbs.mkdir(parents=True)
    shared = thumbs / "shared_v1_320.jpg"
    exclusive = thumbs / "only-one_v1_320.jpg"
    unique = thumbs / f"fid{first_file}_v1_320.jpg"
    shared.write_bytes(b"shared")
    exclusive.write_bytes(b"exclusive")
    unique.write_bytes(b"unique")

    result = remove_archive(db_path, str(cache), first)

    assert result["ok"] is True
    assert shared.exists()  # still used by /two
    assert not exclusive.exists()
    assert not unique.exists()
    conn = db.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM roots").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM files WHERE root_id=?", (first,)).fetchone()[0] == 0
    assert tuple(conn.execute("SELECT hidden, dup_group_id FROM files WHERE id=?", (second_file,)).fetchone()) == (0, None)
    conn.close()
