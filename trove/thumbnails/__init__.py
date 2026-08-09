"""Optional image/video thumbnails for the grid.

If Pillow is available, generate and disk-cache a small JPEG per image; else
return None and the server falls back to serving the original (the browser
scales it). Video thumbnails are a single extracted frame via ffmpeg, if
present on the system. Read-only over originals.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

from ..media.types import VIDEO_EXTS as _CATALOGUED_VIDEO_EXTS
from ..runtime import no_window, tool, tool_env

if TYPE_CHECKING:
    # Pillow is optional and imported inside the functions that need it; this is
    # the name the annotations use, and costs nothing at runtime.
    from PIL.Image import Image as PILImage

logger = logging.getLogger(__name__)

# (x, y, w, h) in the pixels of the frame the detector looked at.
Box = tuple[int, int, int, int]

_PILLOW_READY = False

# Derived from the catalogue's own list rather than written out again, because
# the two drifted: media/types.py has long called .3g2, .flv, .mts, .m2ts and
# .swf video -- so the grid drew a film icon for them -- while this set did
# not, which sent them down the Pillow branch instead of to ffmpeg. Pillow
# cannot open a video, so they got no thumbnail, and the fallback below then
# handed the whole clip to an <img>. A camcorder archive is mostly .mts.
#
# One list means a format the catalogue learns about is a format ffmpeg is
# asked about. Dotted here because the callers hold a Path and compare
# ``suffix``; the catalogue keys off a bare extension column.
VIDEO_EXTS = {f".{ext}" for ext in _CATALOGUED_VIDEO_EXTS}

# Bump when the extraction logic changes (e.g. a different video seek). The
# cache key embeds it, so old thumbnails made by earlier logic are ignored and
# regenerated instead of being served forever — a fid-only key never could.
THUMB_VER = 1


@contextmanager
def _atomic(tp: Path) -> Iterator[Path]:
    """Yield a scratch path to write, then publish it at ``tp`` by rename.

    Every cache file here is published rather than written where readers look
    for it, and that is not fastidiousness. "Already cached?" is answered by
    ``tp.exists()``, a hit is handed straight to the server's ``_send_file``,
    and that stats the file for ``Content-Length`` -- so a thumbnail written in
    place is visible, and servable, from its first zero-length byte. A second
    request arriving in that window answered ``200`` with a truncated body,
    which the browser then cached as a perfectly good image; only a hard reload
    cleared it. The window is wide open in practice: the server is threaded,
    and the cache key is content-addressed, so every byte-identical copy in a
    duplicate group asks for the same file at the same moment.

    ``os.replace`` is atomic on POSIX and Windows, so ``tp`` either does not
    exist or is complete. The scratch name carries pid and thread id, so two
    workers racing on one cache key write to separate files and the later
    rename simply wins -- they produce identical bytes either way.

    An empty scratch file is never published: a writer that failed leaves no
    cache entry at all, rather than a zero-byte one that would be served
    forever as a valid hit.

    The scratch name ends in the target's own extension, and that is not
    cosmetic. One of the writers here is ffmpeg, which chooses its output
    muxer from the filename it is given: a path ending ``.tmp`` names no
    format it knows, so it refused every job before decoding a frame
    ("Unable to choose an output format"). It reports that by exiting
    non-zero rather than by raising, so with ``.tmp`` on the end every video
    thumbnail, every sampled frame behind semantic video indexing, and every
    keyframe behind a video face crop failed in silence. Pillow is told its
    format explicitly and would not have cared; ffmpeg only has the name.
    """
    tp.parent.mkdir(parents=True, exist_ok=True)
    tmp = tp.with_name(f".{tp.name}.{os.getpid()}-{threading.get_ident()}.tmp{tp.suffix}")
    try:
        yield tmp
        if tmp.exists() and tmp.stat().st_size > 0:
            os.replace(tmp, tp)
    finally:
        tmp.unlink(missing_ok=True)


def _cache_key(fid: int, sha256: str | None, rotate: int = 0) -> str:
    # Content-addressed when we have the hash: byte-identical duplicates then map
    # to the same file, so they can never show different frames. Fall back to fid
    # only for not-yet-hashed files. The rotation is part of the key so a photo
    # whose orientation is resolved later doesn't keep serving a sideways
    # thumbnail from before.
    base = sha256 if sha256 else f"fid{fid}"
    return f"{base}_v{THUMB_VER}" + (f"_r{rotate}" if rotate else "")


def _apply_rotation(im: PILImage, rotate: int) -> PILImage:
    """Turn a decoded image clockwise by 0/90/180/270 degrees.

    Uses the lossless transposes rather than ``rotate()`` so no resampling or
    off-by-one padding creeps into a quarter turn.
    """
    from PIL import Image

    transpose = {
        90: Image.Transpose.ROTATE_270,  # PIL names turns counter-clockwise
        180: Image.Transpose.ROTATE_180,
        270: Image.Transpose.ROTATE_90,
    }.get(rotate)
    return im.transpose(transpose) if transpose else im


def _try_pillow() -> tuple[ModuleType, ModuleType] | None:
    """``(PIL.Image, PIL.ImageOps)``, or None when Pillow is not installed.

    Modules rather than the classes themselves, because callers need
    ``Image.open`` and ``ImageOps.exif_transpose``. The checker sees no further
    than ``ModuleType`` past this point -- the trade for keeping Pillow optional
    and imported lazily.
    """
    global _PILLOW_READY
    try:
        from PIL import Image, ImageFile, ImageOps
    except ImportError:
        return None
    if not _PILLOW_READY:
        try:
            import pillow_heif

            pillow_heif.register_heif_opener()
        except Exception:
            # pillow_heif is optional (HEIC support only) and wraps a native
            # libheif binding, so a broken/partial install can fail in more ways
            # than ImportError. Silent on purpose, matching faces/backend.py,
            # pets/backend.py and detect/extract.py: without HEIC support these
            # files simply fail to decode later like any other unreadable file.
            pass
        # Draw as much of a photo as is actually there. Pillow's default is to
        # raise "image file is truncated" when a JPEG's last bytes are missing,
        # which happens to real photographs -- an interrupted copy, a phone
        # pulled off a cable mid-write, a Takeout export that lost its tail --
        # and every browser renders those without complaint. Refusing meant the
        # tile fell back to sending the whole original, which the browser then
        # drew anyway: the same picture, at twenty times the bytes.
        #
        # The flag is process-wide, so a decode anywhere else becomes equally
        # tolerant once the first thumbnail has been made. That is the wanted
        # direction -- a photo worth showing is a photo worth indexing -- and it
        # can only turn a failure into a partial success, never the reverse.
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        _PILLOW_READY = True
    return Image, ImageOps


def _why(stderr: bytes) -> str:
    """The last thing ffmpeg said before giving up.

    Its whole log goes to stderr -- banner, build flags, every stream it
    probed -- and the sentence naming the failure is the last line of it.
    Decoded leniently because the text carries filenames, which are whatever
    the filesystem holds rather than anything guaranteed to be UTF-8.
    """
    lines = [ln for ln in stderr.decode("utf-8", "replace").splitlines() if ln.strip()]
    return lines[-1] if lines else "no output"


def _video_frame(tp: Path, src: Path, size: int, offset: str) -> bool:
    ffmpeg = tool("ffmpeg")
    if ffmpeg is None:
        # Previously this fell through to subprocess.run(None, ...) and was
        # caught below as a TypeError. Same outcome, but named: a machine with
        # no ffmpeg is a supported configuration, not an extraction failure.
        logger.warning("ffmpeg not found; no video thumbnail for %s", src)
        return False
    extracted = False
    with _atomic(tp) as tmp:
        try:
            done = subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-ss",
                    offset,
                    "-i",
                    str(src),
                    "-frames:v",
                    "1",
                    "-vf",
                    f"scale={size}:-1:force_original_aspect_ratio=decrease",
                    "-q:v",
                    "4",
                    str(tmp),
                ],
                stdout=subprocess.DEVNULL,
                # Kept rather than discarded: ffmpeg reports a refused job by
                # exiting non-zero, not by raising, so with this on DEVNULL the
                # only trace of one was this function returning False -- which
                # every caller reads as "this video has no frame there". A
                # scratch filename it could choose no muxer for hid behind that
                # for a release, and no log line anywhere said why.
                stderr=subprocess.PIPE,
                timeout=20,
                # The bundled ffmpeg is a shared build and cannot find its own
                # libav* without this; see runtime.tool_env. Harmless for a PATH
                # ffmpeg, which ignores a library path it does not need.
                env=tool_env(),
                **no_window(),
            )
            extracted = tmp.exists() and tmp.stat().st_size > 0
            if done.returncode:
                # Only a non-zero exit, which is what separates "ffmpeg would
                # not do this" from the ordinary case of an offset past the end
                # of a short clip: that exits 0 having written nothing, and
                # video_frames_for is built to skip it. Warning on "no file"
                # instead would put a line in the log for every short video.
                logger.warning(
                    "ffmpeg exited %s extracting a frame of %s at %s: %s",
                    done.returncode,
                    src,
                    offset,
                    _why(done.stderr),
                )
        except Exception as exc:
            # Mostly subprocess.TimeoutExpired (the 20s timeout above), plus
            # whatever a codec ffmpeg cannot read raises. Either way there is no
            # frame. One line, no exc_info: frames_for() calls this once per
            # offset per video, so a bad codec would otherwise write a traceback
            # for every offset of every affected video.
            logger.warning("ffmpeg frame extraction failed for %s at %s: %s", src, offset, exc)
    return extracted


def _thumb_video(tp: Path, src: Path, size: int) -> Path | None:
    # Try a frame a second in first (skips black opening frames on many
    # clips); very short videos have no frame there, so fall back to t=0.
    if _video_frame(tp, src, size, "00:00:01") or _video_frame(tp, src, size, "00:00:00"):
        return tp
    return None


# Frames used for semantic video indexing (see services/semantic.py): a handful of
# sampled frames sent to Voyage as one multi-image input, instead of the raw
# video file, so a video's request stays token-sized like a photo. Cached the
# same way as the grid thumbnail so a repeated semantic pass over an
# unchanged video costs no extra ffmpeg calls. Bump when extraction changes.
SEMANTIC_FRAME_VER = 1


def _semantic_frame_key(fid: int, sha256: str | None, index: int, rotate: int = 0) -> str:
    base = sha256 if sha256 else f"fid{fid}"
    return f"{base}_sf{index}_v{SEMANTIC_FRAME_VER}" + (f"_r{rotate}" if rotate else "")


def video_frames_for(
    cache_dir: str,
    fid: int,
    src: Path,
    offsets: list[str],
    size: int = 1024,
    sha256: str | None = None,
    rotate: int = 0,
) -> list[Path]:
    """Disk-cached frames at each of ``offsets``, reusing ``_video_frame``.

    An offset ffmpeg can't produce (past a very short clip's end) is skipped
    rather than failing the whole video, so a video yields whatever frames it
    can instead of nothing at all. Returns ``[]`` if ffmpeg is missing or
    every offset fails -- the caller treats that as a clean, permanent skip.
    Read-only over the original.
    """
    out = []
    for i, offset in enumerate(offsets):
        tp = (
            Path(cache_dir)
            / "semantic_frames"
            / f"{_semantic_frame_key(fid, sha256, i, rotate)}_{size}.jpg"
        )
        if tp.exists() or _video_frame(tp, src, size, offset):
            out.append(tp)
    return out


# Frames the fused detect stage (detect/extract.py) samples from a video and
# runs the face/pet detectors on. Unlike video_frames_for (keyed on a list
# index, resolved once per indexing run), a detection's ``frame_offset`` is
# stored in the DB and looked up months later purely from that string -- so
# the cache key here is derived from the offset itself, not a position in a
# list, and must be reproducible from the stored row alone. Bump when
# extraction changes.
DETECT_FRAME_VER = 1


def _sanitize_offset(offset: str) -> str:
    # ffmpeg -ss offsets look like "00:00:12.500" -- colons and dots are not
    # portable in filenames on every filesystem, so fold them to underscores.
    return offset.replace(":", "-").replace(".", "_")


def detect_frame_for(
    cache_dir: str, fid: int, src: Path, offset: str, size: int, sha256: str | None = None
) -> Path | None:
    """Disk-cached keyframe extracted at ``offset``, for the fused detect stage.

    Re-derivable from ``offset`` alone (plus fid/sha/size), so a crop served
    long after detection can regenerate the exact frame a stored detection's
    box was measured in. Returns None if ffmpeg fails or is missing --
    callers treat that as a clean, permanent skip. Read-only over the
    original.
    """
    base = sha256 if sha256 else f"fid{fid}"
    tp = (
        Path(cache_dir)
        / "detect_frames"
        / f"{base}_{_sanitize_offset(offset)}_v{DETECT_FRAME_VER}_{size}.jpg"
    )
    if tp.exists():
        return tp
    return tp if _video_frame(tp, src, size, offset) else None


# Bump when the face-crop framing changes so old crops are regenerated.
FACE_THUMB_VER = 2


def _face_key(fid: int, sha256: str | None, box: Box, rotate: int = 0, variant: str = "") -> str:
    x, y, w, h = box
    base = sha256 if sha256 else f"fid{fid}"
    return (
        f"{base}_{x}_{y}_{w}_{h}_fv{FACE_THUMB_VER}"
        + (f"_r{rotate}" if rotate else "")
        + (f"_{variant}" if variant else "")
    )


def face_thumb_for(
    cache_dir: str,
    face_id: int,
    src: Path,
    box: Box,
    sha256: str | None = None,
    size: int = 200,
    rotate: int = 0,
    variant: str = "",
) -> Path | None:
    """A padded square crop around one face box, disk-cached. Content-addressed
    (sha + box) so the same face in byte-identical duplicates shares one crop.
    Read-only over the original; returns None if Pillow is unavailable.

    ``rotate`` is applied before cropping: boxes are stored in the frame the
    detector actually looked at, which for a sideways-stored photo is the turned
    one. ``variant`` distinguishes crops that would otherwise collide on the
    same (sha, box) key -- namely two different frames of one video that
    happen to produce identical boxes; callers pass the frame's offset string
    for video detections, empty for photos."""
    tp = (
        Path(cache_dir) / "faces" / f"{_face_key(face_id, sha256, box, rotate, variant)}_{size}.jpg"
    )
    if tp.exists():
        return tp
    pil = _try_pillow()
    if pil is None:
        return None
    Image, ImageOps = pil
    try:
        with _atomic(tp) as tmp, Image.open(src) as im:
            im = _apply_rotation(ImageOps.exif_transpose(im), rotate).convert("RGB")
            W, H = im.size
            x, y, w, h = box
            cx, cy = x + w / 2, y + h / 2
            # Keep the crop square even when the face is close to an image
            # edge.  Clamping each edge independently made those crops
            # rectangular, leaving visible side gutters in square UI slots.
            side = max(1, min(round(max(w, h) * 1.6), W, H))
            left = max(0, min(round(cx - side / 2), W - side))
            top = max(0, min(round(cy - side / 2), H - side))
            right, bottom = left + side, top + side
            if right <= left or bottom <= top:
                return None
            crop = im.crop((left, top, right, bottom))
            resampling = getattr(Image, "Resampling", Image).LANCZOS
            crop = crop.resize((size, size), resampling)
            crop.save(tmp, "JPEG", quality=82)
        return tp
    except Exception:
        logger.warning("face thumbnail failed for face_id=%s src=%s", face_id, src, exc_info=True)
        return None


def _thumb_pdf(tp: Path, src: Path, size: int) -> Path | None:
    """The first page of a PDF, rendered to the thumbnail cache.

    A document is a picture of a page as far as a grid is concerned, and without
    this every PDF in the archive is an identical grey icon -- so the one thing
    that tells two contracts apart, what is actually printed on them, is the one
    thing not shown.

    Uses the PDFium already vendored for the text stage (`trove/text/pdf.py`),
    and returns None when that extra is not installed, which callers already
    treat as "no thumbnail" rather than as a failure.
    """
    from ..text import pdf as pdf_reader

    if not pdf_reader.available():
        return None
    try:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(src)
        try:
            if len(doc) == 0:
                return None
            page = doc[0]
            # Scale so the longer side lands on `size`; PDFium works in points
            # at 72 dpi, and rendering at 1:1 would give a ~600 px page we then
            # throw most of away.
            box = page.get_size()
            scale = max(0.1, min(4.0, size / max(box[0], box[1])))
            bitmap = page.render(scale=scale)
            image = bitmap.to_pil()
            with _atomic(tp) as tmp:
                image.convert("RGB").save(tmp, "JPEG", quality=80)
        finally:
            doc.close()
        return tp
    except Exception:
        logger.warning("pdf thumbnail failed for src=%s", src, exc_info=True)
        return None


def thumb_for(
    cache_dir: str, fid: int, src: Path, size: int = 320, sha256: str | None = None, rotate: int = 0
) -> Path | None:
    tp = Path(cache_dir) / "thumbs" / f"{_cache_key(fid, sha256, rotate)}_{size}.jpg"
    if tp.exists():
        return tp
    if src.suffix.lower() in VIDEO_EXTS:
        return _thumb_video(tp, src, size)
    if src.suffix.lower() == ".pdf":
        return _thumb_pdf(tp, src, size)
    pil = _try_pillow()
    if pil is None:
        return None
    Image, ImageOps = pil
    try:
        with _atomic(tp) as tmp, Image.open(src) as im:
            im = _apply_rotation(ImageOps.exif_transpose(im), rotate)
            im.thumbnail((size, size))
            im.convert("RGB").save(tmp, "JPEG", quality=80)
        return tp
    except Exception:
        logger.warning("thumbnail failed for fid=%s src=%s", fid, src, exc_info=True)
        return None


# Bump when the upright full-size rendering changes.
UPRIGHT_VER = 1


def upright_for(
    cache_dir: str, fid: int, src: Path, rotate: int, sha256: str | None = None
) -> Path | None:
    """A full-size copy of a sideways-stored photo, turned upright.

    Only for the viewer, and only when ``rotate`` is non-zero — an untouched
    photo is always served as its own bytes. Re-encoding is the honest cost of
    never writing to the original: the file on disk stays exactly as it was.
    """
    if not rotate:
        return None
    tp = Path(cache_dir) / "upright" / f"{_cache_key(fid, sha256, rotate)}_u{UPRIGHT_VER}.jpg"
    if tp.exists():
        return tp
    pil = _try_pillow()
    if pil is None:
        return None
    Image, ImageOps = pil
    try:
        with _atomic(tp) as tmp, Image.open(src) as im:
            im = _apply_rotation(ImageOps.exif_transpose(im), rotate)
            im.convert("RGB").save(tmp, "JPEG", quality=90)
        return tp
    except Exception:
        logger.warning("upright render failed for fid=%s src=%s", fid, src, exc_info=True)
        return None
