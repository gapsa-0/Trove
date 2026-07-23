import sqlite3

from organize_archive.config import Config
from organize_archive.db import database as db
from organize_archive.dedup import exact


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    root = db.get_or_create_root(conn, "/archive")
    return conn, root


def _file(conn, root, path, sha, size):
    conn.execute(
        """INSERT INTO files(root_id, rel_path, ext, size, mtime, media_type,
                             sha256, first_seen, last_seen)
           VALUES(?, ?, 'jpg', ?, 0, 'image', ?, 'now', 'now')""",
        (root, path, size, sha),
    )


def test_exact_grouping_still_works_without_visual_dependencies():
    conn, root = _db()
    _file(conn, root, "one.jpg", "a" * 64, 10)
    _file(conn, root, "two.jpg", "a" * 64, 10)
    conn.commit()

    stats = exact.run(conn)

    assert (stats.groups, stats.duplicate_files, stats.reclaimable_bytes) == (1, 1, 10)
    assert [r[0] for r in conn.execute("SELECT hidden FROM files ORDER BY id")] == [0, 1]


def test_visual_match_merges_different_encodings_and_keeps_best_image(monkeypatch):
    conn, root = _db()
    _file(conn, root, "large.jpg", "a" * 64, 40)
    _file(conn, root, "small.png", "b" * 64, 10)
    _file(conn, root, "different.jpg", "c" * 64, 20)
    conn.executemany("INSERT INTO media_meta(file_id, width, height) VALUES(?, ?, ?)",
                     [(1, 100, 100), (2, 50, 50)])
    conn.commit()
    monkeypatch.setattr(exact, "_perceptual_hashes",
                        lambda *args, **kwargs: ({1: 0, 2: 0x3F, 3: 2**64 - 1}, 2, 0))

    stats = exact.run(conn, Config())

    assert (stats.groups, stats.duplicate_files, stats.reclaimable_bytes) == (1, 1, 10)
    group = conn.execute("SELECT method, canonical_file_id FROM dup_groups").fetchone()
    assert tuple(group) == ("perceptual", 1)
