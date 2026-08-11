"""Re-encoding a video on its way to the window, and cleaning up after it.

The window plays a short list of formats and an archive of family video is
full of others, so a video it cannot draw is handed to ffmpeg and the window
gets what comes back. Two things about that are worth pinning, and they fail
in opposite directions:

* it has to produce something the window can actually play, from an offset,
  which needs a real ffmpeg and is checked against one;
* it has to stop when nobody is listening. Every abandoned stream holds a
  process encoding a file to a pipe with no reader, and arrowing through a
  folder of .avi files abandons one per press. That half needs no ffmpeg at
  all -- a stand-in that outlives its reader tests it better, because it would
  survive the bug.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from trove.media import transcode
from trove.runtime import no_window, tool, tool_env

needs_ffmpeg = pytest.mark.skipif(
    tool("ffmpeg") is None, reason="re-encoding for playback needs ffmpeg"
)


def _source(path: Path, seconds: float = 6.0) -> Path:
    """A tiny real video, built by the same ffmpeg the app would use."""
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


def _probe(data: bytes, tmp_path: Path, name: str = "probe.mp4") -> dict:
    """What ffprobe makes of some bytes -- the same question the window asks."""
    out = tmp_path / name
    out.write_bytes(data)
    done = subprocess.run(
        [
            str(tool("ffprobe")),
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(out),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env=tool_env(),
        **no_window(),
    )
    return json.loads(done.stdout or "{}")


@needs_ffmpeg
def test_the_re_encoding_is_something_the_window_can_play(tmp_path: Path):
    """H.264 in an MP4, which is the whole point: the formats that reach here
    are the ones the window has no reader for."""
    chunks = transcode.stream(_source(tmp_path / "clip.mp4"))
    assert chunks is not None

    data = b"".join(chunks)

    codecs = {s["codec_type"]: s["codec_name"] for s in _probe(data, tmp_path)["streams"]}
    assert codecs.get("video") == "h264", codecs
    # Fragmented, or the window has nothing to start on: an ordinary MP4 keeps
    # its index in an atom written last, which for a stream never arrives.
    assert data[4:8] == b"ftyp", "no MP4 header at the front of the stream"
    assert b"moof" in data[:16384], "not fragmented; the window cannot begin on this"


@needs_ffmpeg
def test_an_offset_starts_the_stream_where_it_was_asked_to(tmp_path: Path):
    """A pipe cannot be rewound, so the viewer seeks by asking for a new stream
    from a new offset. If that offset were ignored every seek would silently
    replay the video from the top."""
    src = _source(tmp_path / "clip.mp4", seconds=6.0)

    whole = b"".join(transcode.stream(src) or [])
    tail = b"".join(transcode.stream(src, start_s=4.0) or [])

    full = float(_probe(whole, tmp_path, "whole.mp4")["format"]["duration"])
    rest = float(_probe(tail, tmp_path, "tail.mp4")["format"]["duration"])
    assert full == pytest.approx(6.0, abs=0.6), full
    # Four seconds in, about two should be left. Loose bounds: where a seek
    # lands depends on the keyframes, and the claim is "it skipped ahead", not
    # a frame-exact offset.
    assert 1.0 < rest < 3.5, f"seeking to 4s of a 6s clip left {rest}s"


def test_no_ffmpeg_is_answered_rather_than_crashed(monkeypatch, tmp_path: Path):
    """A supported configuration -- the same install that gets no video
    thumbnails -- and the viewer has a panel for it. It must not arrive as an
    exception out of a route."""
    monkeypatch.setattr(transcode, "tool", lambda name: None)

    assert transcode.available() is False
    assert transcode.stream(tmp_path / "absent.avi") is None


# -- cleanup ----------------------------------------------------------------
# A stand-in for ffmpeg that ignores its arguments, writes forever and does not
# die on its own. Real ffmpeg would also outlive an abandoned request, but it
# stops when it reaches the end of a short test clip -- which would let the
# leak these tests are about pass unnoticed.
_FOREVER = (
    "import sys, time\nwhile True:\n sys.stdout.buffer.write(b'x' * 4096)\n time.sleep(0.01)\n"
)


@pytest.fixture
def endless(tmp_path: Path, monkeypatch):
    """Make ``transcode`` spawn the stand-in above instead of ffmpeg."""
    script = tmp_path / "forever.py"
    script.write_text(_FOREVER)
    shim = tmp_path / ("shim.bat" if os.name == "nt" else "shim.sh")
    if os.name == "nt":
        shim.write_text(f'@echo off\r\npython "{script}"\r\n')
    else:
        shim.write_text(f'#!/bin/sh\nexec "{os.sys.executable}" "{script}"\n')
        shim.chmod(0o755)
    monkeypatch.setattr(transcode, "tool", lambda name: str(shim))
    return shim


def _children() -> set[int]:
    """This process's live children, by pid."""
    out = subprocess.run(
        ["ps", "-o", "pid=", "--ppid", str(os.getpid())], capture_output=True, text=True
    )
    return {int(line) for line in out.stdout.split() if line.strip().isdigit()}


@pytest.mark.skipif(os.name == "nt", reason="pid bookkeeping here is POSIX-only")
def test_closing_the_stream_stops_the_encoder(endless):
    """The leak this is built around.

    The viewer abandons a stream on every arrow press and on every seek, and
    the server closes the generator when the socket dies. That close is the
    only thing standing between a folder of unplayable video and one encoder
    per file it was arrowed past, each writing to a pipe nobody reads.
    """
    before = _children()
    chunks = transcode.stream(Path("whatever.avi"))
    assert chunks is not None
    assert next(chunks)  # started, and producing
    spawned = _children() - before
    assert spawned, "nothing was spawned; the test is not testing anything"

    chunks.close()

    for _ in range(50):  # it is killed, not asked politely; this is quick
        if not (_children() & spawned):
            break
        time.sleep(0.1)
    assert not (_children() & spawned), "the encoder outlived the stream that owned it"


@pytest.mark.skipif(os.name == "nt", reason="pid bookkeeping here is POSIX-only")
def test_an_exhausted_stream_leaves_nothing_running(endless, monkeypatch):
    """The ordinary ending, which takes the same path as the abandoned one:
    a reader that stops reading must not depend on being polite about it."""
    before = _children()
    chunks = transcode.stream(Path("whatever.avi"))
    assert chunks is not None
    next(chunks)
    spawned = _children() - before

    del chunks  # dropped without closing, the way an abandoned request is

    for _ in range(50):
        if not (_children() & spawned):
            break
        time.sleep(0.1)
    assert not (_children() & spawned), "a dropped stream left its encoder running"
