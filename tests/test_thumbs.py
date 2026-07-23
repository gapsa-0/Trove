from pathlib import Path

import pytest

Image = pytest.importorskip("PIL.Image")

from organize_archive.gui.thumbs import face_thumb_for


def test_face_thumbnail_stays_square_at_image_edge(tmp_path: Path):
    source = tmp_path / "source.jpg"
    Image.new("RGB", (400, 240), "red").save(source)

    result = face_thumb_for(
        str(tmp_path / "cache"),
        face_id=1,
        src=source,
        box=(0, 70, 80, 100),
        size=200,
    )

    assert result is not None
    with Image.open(result) as thumbnail:
        assert thumbnail.size == (200, 200)
