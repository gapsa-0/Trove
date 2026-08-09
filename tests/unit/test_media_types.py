"""What the catalogue calls a file, and who else has to agree with it.

``media/types.py`` is the single classification: it decides the ``media_type``
column, which decides the icon a grid draws and which stages will look at the
file at all. A second module keeping its own copy of one of these lists is a
drift waiting to happen, and one already had.
"""

from trove.media.types import (
    ARCHIVE_EXTS,
    AUDIO_EXTS,
    DOCUMENT_EXTS,
    IMAGE_EXTS,
    VIDEO_EXTS,
    media_type,
)


def test_the_thumbnailer_sends_ffmpeg_every_format_the_catalogue_calls_video():
    """A format the catalogue calls video and the thumbnailer does not is one
    the grid marks with a film icon and then asks Pillow to open -- which
    cannot read a video. .3g2, .flv, .mts, .m2ts and .swf were in that gap,
    and .mts is most of a camcorder archive.
    """
    from trove import thumbnails

    assert {f".{ext}" for ext in VIDEO_EXTS} == thumbnails.VIDEO_EXTS


def test_no_extension_is_claimed_by_two_kinds_at_once():
    """media_type returns the first list that matches, so an extension in two
    of them would resolve by the order the checks happen to be written in."""
    lists = {
        "image": IMAGE_EXTS,
        "video": VIDEO_EXTS,
        "audio": AUDIO_EXTS,
        "document": DOCUMENT_EXTS,
        "archive": ARCHIVE_EXTS,
    }
    seen: dict[str, str] = {}
    for kind, exts in lists.items():
        for ext in exts:
            assert ext not in seen, f".{ext} is both {seen.get(ext)} and {kind}"
            seen[ext] = kind


def test_an_extension_is_read_the_same_however_it_is_written():
    for spelling in ("MP4", ".mp4", "mp4", ".MP4"):
        assert media_type(spelling) == "video", spelling


def test_a_file_with_no_extension_is_not_guessed_at():
    assert media_type("") == "other"
