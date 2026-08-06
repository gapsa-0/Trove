"""Animated files are never fingerprinted, so they group by bytes alone.

Pillow hands back frame 0 of an animation and nothing else, so a perceptual
hash of a GIF describes how it OPENS, not what it is. Two unrelated animations
that share a title card or a fade-in from white therefore fingerprint
identically -- measured at distance 0 -- and dedup hid one as a copy of the
other. Video is already exact-match only for exactly this reason; an animated
GIF is a clip wearing an image extension, and belongs on the same side of that
line.

These run the real decode rather than a monkeypatched hash table, because the
rule being checked is a property of what Pillow returns.
"""

import factories
import pytest

from trove.config import Config
from trove.db import database as db
from trove.dedup import exact

pytestmark = pytest.mark.skipif(
    not exact.perceptual_available(), reason="perceptual matching needs Pillow and ImageHash"
)


def _frames(seed, size=(120, 120)):
    """Three frames of noise: distinctive enough to fingerprint apart."""
    import random

    from PIL import Image, ImageDraw

    out = []
    for step in range(3):
        rng = random.Random(f"{seed}-{step}")
        im = Image.new("RGB", size, (255, 255, 255))
        draw = ImageDraw.Draw(im)
        for _ in range(12):
            x, y = rng.randint(0, 90), rng.randint(0, 90)
            draw.ellipse([x, y, x + 30, y + 30], fill=(rng.randint(0, 255),) * 3)
        out.append(im)
    return out


def _write_gif(path, seed, first=None):
    """An animated GIF whose opening frame can be shared with another file."""
    frames = _frames(seed)
    if first is not None:
        frames[0] = _frames(first)[0]
    frames[0].save(
        path, save_all=True, append_images=frames[1:], duration=100, loop=0, format="GIF"
    )


def _archive(tmp_path, files):
    """A catalog over real files on disk, ready for a full dedup run."""
    conn = factories.make_db(tmp_path)
    root = tmp_path / "photos"
    for name, sha in files:
        factories.add_file(conn, rel_path=name, sha256=sha * 64, size=(root / name).stat().st_size)
    conn.commit()
    return conn


def test_two_animations_sharing_a_first_frame_are_not_grouped(tmp_path):
    root = tmp_path / "photos"
    root.mkdir(exist_ok=True)
    _write_gif(root / "one.gif", seed="a")
    _write_gif(root / "two.gif", seed="b", first="a")  # same opening frame, different animation
    conn = _archive(tmp_path, [("one.gif", "a"), ("two.gif", "b")])

    exact.run(conn, Config())

    assert conn.execute("SELECT COUNT(*) FROM dup_groups").fetchone()[0] == 0
    assert [r[0] for r in conn.execute("SELECT hidden FROM files ORDER BY id")] == [0, 0]
    # Nothing cached either: a stored first-frame hash would group them on the
    # next run without the file ever being opened again.
    assert conn.execute("SELECT COUNT(*) FROM perceptual_hashes").fetchone()[0] == 0


def test_an_animation_wearing_a_still_extension_is_caught_too(tmp_path):
    """The rule is asked of the decoded image, not the file name -- Pillow reads
    the magic bytes and opens a GIF correctly however it is named."""
    root = tmp_path / "photos"
    root.mkdir(exist_ok=True)
    _write_gif(root / "one.gif", seed="a")
    _write_gif(root / "mislabelled.png", seed="b", first="a")
    conn = _archive(tmp_path, [("one.gif", "a"), ("mislabelled.png", "b")])

    exact.run(conn, Config())

    assert conn.execute("SELECT COUNT(*) FROM dup_groups").fetchone()[0] == 0


def test_a_still_image_is_still_fingerprinted(tmp_path):
    """The guard is about animation, not about GIF: a single-frame image goes on
    being matched exactly as before, including a single-frame GIF."""

    root = tmp_path / "photos"
    root.mkdir(exist_ok=True)
    still = _frames("a")[0]
    still.save(root / "photo.png")
    still.save(root / "photo.gif", format="GIF")  # same picture, one frame, different encoding
    conn = _archive(tmp_path, [("photo.png", "a"), ("photo.gif", "b")])

    exact.run(conn, Config())

    group = conn.execute("SELECT method, member_count FROM dup_groups").fetchone()
    assert tuple(group) == ("perceptual", 2)


def test_a_stale_first_frame_fingerprint_is_cleared_on_upgrade(tmp_path):
    """Databases fingerprinted before this rule carry first-frame hashes for
    animated files, and the pass answers from that cache whenever the source SHA
    still matches -- so the old values would keep grouping GIFs forever without
    the file being opened again. The migration retires them."""
    conn = factories.make_db(tmp_path)
    factories.add_file(conn, rel_path="clip.gif", sha256="a" * 64)
    factories.add_file(conn, rel_path="photo.jpg", sha256="b" * 64)
    for file_id in (1, 2):
        conn.execute(
            """INSERT INTO perceptual_hashes(file_id, source_sha256, algorithm, hash, created_at)
               VALUES(?, ?, 'phash64', '0f0f0f0f0f0f0f0f', '2026-01-01')""",
            (file_id, ("a" if file_id == 1 else "b") * 64),
        )
    conn.execute("PRAGMA user_version=15")
    conn.commit()
    conn.close()

    conn = db.connect(tmp_path / "archive.db")
    db.init_db(conn)

    # The GIF's stale fingerprint is gone; the photograph's expensive one stays.
    kept = [r[0] for r in conn.execute("SELECT file_id FROM perceptual_hashes ORDER BY file_id")]
    assert kept == [2]
