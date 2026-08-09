from pathlib import Path

import pytest

Image = pytest.importorskip("PIL.Image")

# Must stay below the importorskip above: this module must not be imported
# when Pillow is absent.
from trove.thumbnails import face_thumb_for, thumb_for, upright_for  # noqa: E402


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


def _corners(source: Path):
    """A picture whose four quadrants are each a different colour."""
    im = Image.new("RGB", (400, 240))
    im.paste(Image.new("RGB", (200, 120), "red"), (0, 0))  # top-left
    im.paste(Image.new("RGB", (200, 120), "lime"), (200, 0))  # top-right
    im.paste(Image.new("RGB", (200, 120), "blue"), (0, 120))  # bottom-left
    im.paste(Image.new("RGB", (200, 120), "yellow"), (200, 120))  # bottom-right
    im.save(source)


def test_a_sideways_photo_is_thumbnailed_upright(tmp_path: Path):
    """rotate is clockwise, so what was the top-left ends up top-right."""
    source = tmp_path / "source.jpg"
    _corners(source)

    result = thumb_for(str(tmp_path / "cache"), 1, source, size=200, rotate=90)

    assert result is not None
    with Image.open(result) as thumbnail:
        assert thumbnail.width < thumbnail.height  # landscape -> portrait
        w, _h = thumbnail.size
        assert thumbnail.getpixel((w - 10, 10))[0] > 200  # red, now top-right
        assert thumbnail.getpixel((10, 10))[2] > 200  # blue, now top-left


def test_rotated_and_unrotated_thumbnails_do_not_share_a_cache_entry(tmp_path: Path):
    """A photo whose orientation is resolved later must not keep serving the
    sideways thumbnail made before."""
    source = tmp_path / "source.jpg"
    _corners(source)
    cache = str(tmp_path / "cache")

    plain = thumb_for(cache, 1, source, size=200, sha256="abc")
    turned = thumb_for(cache, 1, source, size=200, sha256="abc", rotate=90)

    assert plain != turned
    with Image.open(plain) as a, Image.open(turned) as b:
        assert a.size[0] > a.size[1] and b.size[0] < b.size[1]


def test_a_face_crop_cuts_from_the_upright_frame(tmp_path: Path):
    """Boxes are stored in the frame the detector looked at, so the crop has to
    rotate before it cuts — otherwise it lands on the wrong part of the photo."""
    source = tmp_path / "source.jpg"
    _corners(source)

    # After a 90 degree turn the picture is 240x400 and its top-left is blue.
    result = face_thumb_for(
        str(tmp_path / "cache"), 1, source, box=(10, 10, 60, 60), size=100, rotate=90
    )

    assert result is not None
    with Image.open(result) as crop:
        assert crop.getpixel((50, 50))[2] > 200  # blue


@pytest.mark.parametrize("deg", [90, 180, 270])
def test_detection_and_display_turn_a_photo_the_same_way(deg):
    """Detection turns arrays with numpy, display turns images with Pillow. If
    the two ever disagree, every stored box lands on the wrong part of the
    photo — so pin them against each other."""
    np = pytest.importorskip("numpy")
    from trove.detect.geometry import rotate_image
    from trove.thumbnails import _apply_rotation

    im = Image.new("RGB", (40, 24))
    im.paste(Image.new("RGB", (20, 12), "red"), (0, 0))
    im.paste(Image.new("RGB", (20, 12), "lime"), (20, 0))
    im.paste(Image.new("RGB", (20, 12), "blue"), (0, 12))

    assert np.array_equal(np.asarray(_apply_rotation(im, deg)), rotate_image(np.asarray(im), deg))


def test_a_photo_missing_its_last_bytes_is_still_thumbnailed(tmp_path: Path):
    """Real photographs arrive truncated -- an interrupted copy, a phone pulled
    off a cable, a Takeout export that lost its tail -- and Pillow's default is
    to raise on them where every browser just draws what is there. Refusing
    meant the tile fell back to sending the whole original, which the browser
    then drew anyway: the same picture, at many times the bytes."""
    whole = tmp_path / "whole.jpg"
    Image.new("RGB", (1200, 900), (200, 90, 40)).save(whole, "JPEG", quality=90)
    cut_short = tmp_path / "cut-short.jpg"
    cut_short.write_bytes(whole.read_bytes()[:-400])

    result = thumb_for(str(tmp_path / "cache"), 1, cut_short, size=320)

    assert result is not None, "no thumbnail; the route would send the original instead"
    assert result.stat().st_size < cut_short.stat().st_size
    with Image.open(result) as thumbnail:
        thumbnail.load()
        assert max(thumbnail.size) == 320


def test_a_file_that_is_not_a_picture_at_all_still_has_no_thumbnail(tmp_path: Path):
    """Tolerating a missing tail must not turn into tolerating anything: a file
    the decoder cannot identify has nothing to draw, and the route needs the
    None to know to answer 404 rather than send it."""
    not_a_picture = tmp_path / "notes.jpg"
    not_a_picture.write_bytes(b"UUUU" * 2048)

    assert thumb_for(str(tmp_path / "cache"), 1, not_a_picture) is None


def test_an_upright_photo_is_never_re_encoded(tmp_path: Path):
    """Rotation zero means the viewer gets the original bytes, untouched."""
    source = tmp_path / "source.jpg"
    _corners(source)

    assert upright_for(str(tmp_path / "cache"), 1, source, 0) is None
    assert upright_for(str(tmp_path / "cache"), 1, source, 90) is not None
