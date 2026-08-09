"""A video gets a real frame out of ffmpeg, and lands in the cache as a JPEG.

The tier had no video coverage at all, which is how a change to where the
cache file is *written* could stop every video thumbnail in the app without a
single test noticing: ffmpeg chooses its output muxer from the filename's
extension, so handing it a scratch path ending in ``.tmp`` makes it refuse the
job before it decodes anything. It fails by exiting non-zero, not by raising,
so nothing upstream saw it either.

These run only where ffmpeg does. That is the same condition the feature has --
``thumbnails._video_frame`` treats a missing ffmpeg as a supported
configuration -- so a machine without one sees skips rather than failures.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from trove import thumbnails
from trove.runtime import no_window, tool, tool_env

pytestmark = pytest.mark.skipif(tool("ffmpeg") is None, reason="video thumbnails need ffmpeg")

Image = pytest.importorskip("PIL.Image")


def _video(path: Path, seconds: float = 2.0) -> Path:
    """A tiny real MP4, built by the same ffmpeg the app would use."""
    subprocess.run(
        [
            str(tool("ffmpeg")),
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size=160x120:rate=10:duration={seconds}",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=60,
        env=tool_env(),
        check=True,
        **no_window(),
    )
    return path


def test_a_video_is_thumbnailed_from_an_extracted_frame(tmp_path: Path):
    src = _video(tmp_path / "clip.mp4")

    result = thumbnails.thumb_for(str(tmp_path / "cache"), 1, src, size=320, sha256="deadbeef")

    assert result is not None, "no frame came back; ffmpeg wrote nothing"
    assert result.exists() and result.stat().st_size > 0
    with Image.open(result) as im:
        im.load()  # a truncated or non-JPEG body raises here
        assert im.format == "JPEG"
        assert max(im.size) <= 320


def test_a_clip_shorter_than_the_preferred_seek_still_gets_a_frame(tmp_path: Path):
    """_thumb_video seeks to one second first to skip black opening frames, so
    a clip that ends before then has to fall back to the very first frame."""
    src = _video(tmp_path / "blink.mp4", seconds=0.4)

    result = thumbnails.thumb_for(str(tmp_path / "cache"), 2, src, sha256="cafe")

    assert result is not None and result.stat().st_size > 0


def test_the_sampled_frames_a_video_is_indexed_from_are_produced(tmp_path: Path):
    """video_frames_for backs semantic indexing: returning [] is recorded as a
    permanent 'unsupported video' skip, so a video it silently fails on is
    never offered to the indexer again."""
    src = _video(tmp_path / "clip.mp4")

    frames = thumbnails.video_frames_for(
        str(tmp_path / "cache"), 3, src, ["00:00:00", "00:00:01"], size=128, sha256="beef"
    )

    assert len(frames) == 2
    assert all(f.stat().st_size > 0 for f in frames)


def test_the_keyframe_a_video_detection_is_re_cut_from_is_produced(tmp_path: Path):
    """detect_frame_for is re-derived from a stored offset months later, so a
    face box measured in a video frame can be cropped again."""
    src = _video(tmp_path / "clip.mp4")

    frame = thumbnails.detect_frame_for(
        str(tmp_path / "cache"), 4, src, "00:00:01.500", 320, sha256="feed"
    )

    assert frame is not None and frame.stat().st_size > 0
