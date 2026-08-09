"""A cache file is never visible until it is complete.

The bug: thumbnails were written straight to the path readers check. "Already
cached?" is ``tp.exists()``, a hit goes to the server's ``_send_file``, and
that stats the file for ``Content-Length`` -- so a request arriving while
another thread was still writing got a ``200`` with a truncated body, which the
browser cached as a good image. Only a hard reload cleared it. The server is
threaded and the cache key is content-addressed, so every byte-identical copy
in a duplicate group asks for the same file at the same moment: the window was
hit routinely, not rarely.

These assert the invariant that fixes it -- the cache path either does not
exist or is complete -- rather than trying to lose a race on purpose, which
would be flaky by construction.
"""

import threading

import pytest

from trove import thumbnails


@pytest.fixture
def cache(tmp_path):
    return tmp_path / "cache"


def _jpeg(path, size=(64, 64)):
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (90, 120, 150)).save(path, "JPEG")
    return path


def test_the_cache_path_does_not_exist_until_the_write_is_finished(cache):
    tp = cache / "thumbs" / "abc_v1_320.jpg"

    with thumbnails._atomic(tp) as tmp:
        tmp.write_bytes(b"partial")
        # The whole point: a reader checking tp right now finds nothing, and
        # goes on to generate its own copy rather than serving these 7 bytes.
        assert not tp.exists()

    assert tp.read_bytes() == b"partial"


def test_the_scratch_path_keeps_the_extension_the_cache_file_will_have(cache):
    """ffmpeg picks its output muxer from the filename, so a scratch path
    ending ``.tmp`` names no format it knows and it refuses the job before
    decoding anything -- by exiting non-zero, which nothing here would see.
    Asserted separately from the video tests (tests/integration/
    test_thumbs_video.py) because those skip where ffmpeg is absent, and this
    invariant has to hold on a machine that cannot run them."""
    tp = cache / "thumbs" / "abc_v1_320.jpg"

    with thumbnails._atomic(tp) as tmp:
        assert tmp.suffix == tp.suffix == ".jpg"


def test_a_failed_write_leaves_no_cache_entry(cache):
    tp = cache / "thumbs" / "abc_v1_320.jpg"

    with pytest.raises(RuntimeError), thumbnails._atomic(tp) as tmp:
        tmp.write_bytes(b"half an image")
        raise RuntimeError("decoder blew up")

    assert not tp.exists()
    assert list(tp.parent.iterdir()) == [], "scratch file left behind"


def test_an_empty_write_is_never_published(cache):
    """A writer that produced nothing -- ffmpeg failing on a codec, say --
    must not leave a zero-byte file, which every later request would serve as a
    valid cache hit forever."""
    tp = cache / "thumbs" / "abc_v1_320.jpg"

    with thumbnails._atomic(tp) as tmp:
        tmp.touch()

    assert not tp.exists()
    assert list(tp.parent.iterdir()) == []


def test_two_threads_racing_on_one_key_both_get_a_whole_thumbnail(cache, tmp_path):
    """The duplicate-group case: two ids, one content-addressed cache path,
    both requested at once."""
    src = _jpeg(tmp_path / "photo.jpg", size=(1200, 900))
    seen, barrier = [], threading.Barrier(2)

    def request(fid):
        barrier.wait()
        seen.append(thumbnails.thumb_for(str(cache), fid, src, sha256="deadbeef"))

    threads = [threading.Thread(target=request, args=(i,)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    from PIL import Image

    assert len(seen) == 2
    for path in seen:
        assert path is not None and path.stat().st_size > 0
        with Image.open(path) as im:
            im.load()  # a truncated JPEG raises here
    assert not list((cache / "thumbs").glob("*.tmp")), "scratch file left behind"
