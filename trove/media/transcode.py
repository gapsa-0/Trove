"""Re-encode a video, as it is sent, into something the window can play.

The player built into the window reads a short list of formats. ffmpeg reads
nearly everything, and Trove already runs it for every video thumbnail, so the
gap between the two is closable without shipping anything new: hand ffmpeg the
file and hand the window what comes out.

**Streamed, never stored.** The output goes straight from the pipe to the
socket. That is what makes the first frames arrive in about a second on a
1.2 GB source -- there is no pass over the file to wait for -- and it is why
nothing has to be cached, invalidated, or cleaned up afterwards. Measured on a
4-core Ryzen 3 2200G, over the archive this was written for:

    640x480 .avi, 5s        0.6s to first byte, 8.6x realtime
    640x480 .avi, 18.7 min  1.0s to first byte, 9.3x realtime
    320x240 .wmv, 6.4 min   0.5s to first byte, 3.3x realtime

Encoding faster than realtime is the requirement; the margin above is what
keeps the picture ahead of the playhead on the slowest of those. The pipe
supplies the throttle for free -- once the window stops reading, the write
blocks and ffmpeg waits -- so a paused video costs nothing and a watched one
costs about three quarters of a core rather than everything available.

``-ss`` before ``-i`` is what makes seeking bearable: ffmpeg jumps to the
nearest keyframe before decoding anything, so starting 16 minutes in costs the
same as starting at the beginning (0.4-1.0s, flat, at every offset measured).
A seek is a new stream from the new offset, because a pipe cannot be rewound.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Generator
from pathlib import Path

from ..runtime import no_window, tool, tool_env

logger = logging.getLogger(__name__)

# What comes out: H.264 and AAC in a fragmented MP4.
#
# Fragmented is the part that matters. An ordinary MP4 carries its index in a
# `moov` atom written last, once the durations are known -- unplayable until
# the final byte arrives, which for a stream is never. `frag_keyframe` writes
# self-contained fragments as it goes and `empty_moov` puts a header at the
# front, so the window can start on the first fragment.
#
# `veryfast` rather than a slower preset: this encode is racing a playhead, not
# producing a master. At `medium` the 320x240 .wmv above fell under realtime,
# which is a stall in the picture; nobody watching gains anything from the
# smaller file that would have been.
_ARGS = (
    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
    "-c:a", "aac", "-b:a", "128k",
    "-movflags", "frag_keyframe+empty_moov+default_base_moof",
    "-f", "mp4", "pipe:1",
)  # fmt: skip

CONTENT_TYPE = "video/mp4"
_CHUNK = 64 * 1024


def available() -> bool:
    """Whether there is an ffmpeg to run at all.

    An install without one is supported -- it is the same install that gets no
    video thumbnails -- so the caller asks first and says so, rather than
    offering a button that fails.
    """
    return tool("ffmpeg") is not None


def stream(src: Path, start_s: float = 0.0) -> Generator[bytes] | None:
    """Yield an H.264/MP4 re-encoding of ``src``, beginning ``start_s`` in.

    ``None`` when there is no ffmpeg. Otherwise a generator that owns the
    process for as long as it is iterated: closing it -- which is what the
    server does when the socket dies, and what a garbage collector does if the
    request is abandoned -- kills ffmpeg. Without that, arrowing through a
    folder of .avi files would leave an encoder running for each one.
    """
    ffmpeg = tool("ffmpeg")
    if ffmpeg is None:
        logger.warning("no ffmpeg; cannot re-encode %s for playback", src)
        return None
    # -ss ahead of -i seeks the *input*: ffmpeg skips to the keyframe before
    # the offset without decoding what it passes over. After -i it would decode
    # and throw away everything up to the offset, which is the difference
    # between a seek costing a second and a seek costing a minute.
    seek = ["-ss", f"{start_s:.3f}"] if start_s > 0 else []
    return _pump(
        [ffmpeg, "-v", "error", *seek, "-i", str(src), *_ARGS],
        src,
    )


def _pump(cmd: list[str], src: Path) -> Generator[bytes]:
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        # Discarded, unlike the thumbnail extractor's: nothing here waits for
        # the exit to judge it. A refusal shows up as a short stream, the
        # window reports it as it reports any other unplayable video, and the
        # panel behind this says so. Held in a pipe nobody drains, a chatty
        # encoder would block on a full stderr buffer and stall the picture.
        stderr=subprocess.DEVNULL,
        env=tool_env(),
        **no_window(),
    )
    try:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(_CHUNK)
            if not chunk:
                return
            yield chunk
    finally:
        # Reached on every ending there is: the stream ran out, the socket
        # broke, or the generator was closed under us. ffmpeg is mid-file in
        # two of those three and would otherwise keep encoding to a pipe with
        # no reader.
        if proc.poll() is None:
            proc.kill()
        if proc.stdout is not None:
            proc.stdout.close()
        proc.wait()
