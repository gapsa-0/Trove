"""Thumbnails and the original file, served by bare id (no ``root`` query
param) except for the start-page cover mosaic, which names its archive."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from ... import thumbnails
from ...media import transcode
from ...services import browse, people, pets
from ._request import NOT_FOUND, FileBody, Json, Request, Stream

# How long a browser may reuse a thumbnail without asking again.
#
# Every one of these URLs is a file id, and the bytes behind an id are not
# fixed: the pipeline settles a photo's rotation after the grid has already
# drawn it, hashing changes the cache key from the id to the content, and an
# edited file keeps its id. So "immutable" would be wrong -- a photo caught
# mid-indexing would stay sideways in the browser for the rest of the session.
#
# A minute is chosen against what actually costs: the grid re-renders on every
# screen change and the filmstrip rebuilds on every arrow press, so the same
# hundred tiles are asked for over and over within seconds. Answering none of
# those is the win. Past the minute the ETag makes the question cheap -- a 304
# and no body -- and a thumbnail that did change is picked up then.
MEDIA_CACHE = "private, max-age=60"

# "Nothing can be drawn from this file", kept as long as a picture would be.
#
# The viewer's filmstrip rebuilds on every arrow press and asks for a
# thumbnail of all twenty-five files around the one open. The pictures among
# those come back out of the browser's cache; without this, the files that
# have no picture were the only ones still crossing the wire, on every press,
# for as long as you held the key down.
#
# Deliberately not the shared ``NOT_FOUND``. This is the answer "this file has
# nothing to show", which stays true. "No archive is open yet" and "no such
# id" are also 404s on this route and are *not* about the file at all -- the
# first is a startup race, and caching it would leave a tile blank for a
# minute after the archive it belongs to opened.
NO_PICTURE = Json({"error": "not found"}, 404, cache_control=MEDIA_CACHE)


# -- media serving ----------------------------------------------------
# Thumbnails and originals are requested by bare id with no ``root``
# (the frontend never sends one for these), so they resolve against
# whichever single archive is currently open, the GUI never browses two
# archives' content at once.
def _open_db_and_cache(req: Request) -> tuple[str, str] | tuple[None, None]:
    rid = req.open_root_id
    if rid is None:
        return None, None
    return req.db(rid), req.cache(rid)


def thumb(req: Request) -> FileBody | Json:
    """A file's thumbnail, generated and cached on first request."""
    fid = int(req.path.rsplit("/", 1)[1])
    db_path, cache_dir = _open_db_and_cache(req)
    info = browse.media_source(db_path, fid) if db_path else None
    if info is None:
        return NOT_FOUND
    # _open_db_and_cache returns its pair together: cache_dir is None exactly
    # when db_path is, and a non-None db_path is what let media_source find
    # ``info`` above, so cache_dir is a str here too.
    cache_dir = cast(str, cache_dir)
    src, sha256, rotate = info
    tp = thumbnails.thumb_for(cache_dir, fid, src, sha256=sha256, rotate=rotate)
    return _thumb_body(tp, src)


# What a browser will actually paint inside an ``<img>``. Named for that and
# not for "images", because the two are different sets: TIFF and HEIC are
# photographs no desktop browser decodes, while SVG is not a photograph and
# every browser draws it. Wider than the catalogue's IMAGE_EXTS in places and
# narrower in others, so it is written out here rather than derived.
_BROWSER_RENDERS = {
    ".jpg",
    ".jpeg",
    ".jfif",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".avif",
    ".ico",
    ".svg",
}


def _thumb_body(tp: Path | None, src: Path) -> FileBody | Json:
    """The generated thumbnail, or the original where a browser can draw it.

    The fallback exists for one case and is worth keeping for it: a photo the
    decoder here refuses -- a JPEG whose last bytes are missing, most often --
    which every browser renders anyway. Handing over the original is how the
    tile shows a picture instead of an icon.

    Which is why this asks whether the *browser* can decode the file, rather
    than whether we happen to know it as a video or a PDF. Under that older
    test everything else fell through to "send the whole file": a .docx, a
    camera RAW, a voice note, a backup .zip. None of them can become a
    picture, so the tile broke regardless -- after downloading the file to
    find out. The viewer's filmstrip asks for a thumbnail of every neighbour
    in the gallery, and this archive holds .zip files of 29 GB; with six
    connections per origin, one of those answers stalls every other image on
    the page behind it.

    A file that cannot be drawn is simply absent, which is what the grid's own
    fallback icon is for.
    """
    if tp:
        return FileBody(tp, cache_control=MEDIA_CACHE)
    if src.suffix.lower() not in _BROWSER_RENDERS:
        return NO_PICTURE
    return FileBody(src, cache_control=MEDIA_CACHE)


def archive_thumb(req: Request) -> FileBody | Json:
    """A thumbnail scoped to a named archive, for the start-page cover mosaic where
    nothing is 'open' yet."""
    parts = req.path.split("/")  # ['', 'archivethumb', root_id, file_id]
    if not (len(parts) == 4 and parts[2].isdigit() and parts[3].isdigit()):
        return NOT_FOUND
    root_id, fid = int(parts[2]), int(parts[3])
    # Root-scoped thumbnail for the start-page cover mosaic: the picker shows
    # several archives at once with none "open", so the id alone (as /thumb/
    # uses) is not enough, the archive is named explicitly here.
    if req.cfg.archive_path(root_id) is None:
        return NOT_FOUND
    db_path, cache_dir = req.db(root_id), req.cache(root_id)
    info = browse.media_source(db_path, fid)
    if info is None:
        return NOT_FOUND
    src, sha256, rotate = info
    tp = thumbnails.thumb_for(cache_dir, fid, src, sha256=sha256, rotate=rotate)
    return _thumb_body(tp, src)


def face_thumb(req: Request) -> FileBody | Json:
    """A cropped face thumbnail, cut from the source photo or (for a video
    detection) its re-derived keyframe."""
    face_id = int(req.path.rsplit("/", 1)[1])
    db_path, cache_dir = _open_db_and_cache(req)
    info = people.face_crop_source(db_path, face_id) if db_path else None
    if info is None:
        return NOT_FOUND
    # See thumb() above: info non-None proves cache_dir is a str too.
    cache_dir = cast(str, cache_dir)
    src, sha256, box, rotate, frame_offset, _media_type, file_id = info
    if frame_offset is not None:
        # A video detection's box was measured in the extracted keyframe,
        # already upright (ffmpeg applies container rotation), so the
        # crop is cut from that same re-derived frame, not the video file
        # itself, and never rotated again.
        frame = thumbnails.detect_frame_for(
            cache_dir, file_id, src, frame_offset, req.cfg.detect_video_frame_px, sha256=sha256
        )
        if frame is None:
            return Json({"error": "frame unavailable"}, 404)
        tp = thumbnails.face_thumb_for(
            cache_dir, face_id, frame, box, sha256=sha256, rotate=0, variant=frame_offset
        )
        return FileBody(tp if tp else frame, cache_control=MEDIA_CACHE)
    tp = thumbnails.face_thumb_for(cache_dir, face_id, src, box, sha256=sha256, rotate=rotate)
    return FileBody(tp if tp else src, cache_control=MEDIA_CACHE)


def animal_thumb(req: Request) -> FileBody | Json:
    """A cropped pet/animal thumbnail, cut from the source photo or (for a video
    detection) its re-derived keyframe."""
    detection_id = int(req.path.rsplit("/", 1)[1])
    db_path, cache_dir = _open_db_and_cache(req)
    info = pets.animal_crop_source(db_path, detection_id) if db_path else None
    if info is None:
        return NOT_FOUND
    # See thumb() above: info non-None proves cache_dir is a str too.
    cache_dir = cast(str, cache_dir)
    src, sha256, box, rotate, frame_offset, _media_type, file_id = info
    if frame_offset is not None:
        frame = thumbnails.detect_frame_for(
            cache_dir, file_id, src, frame_offset, req.cfg.detect_video_frame_px, sha256=sha256
        )
        if frame is None:
            return Json({"error": "frame unavailable"}, 404)
        tp = thumbnails.face_thumb_for(
            cache_dir, detection_id, frame, box, sha256=sha256, rotate=0, variant=frame_offset
        )
        return FileBody(tp if tp else frame, cache_control=MEDIA_CACHE)
    tp = thumbnails.face_thumb_for(cache_dir, detection_id, src, box, sha256=sha256, rotate=rotate)
    return FileBody(tp if tp else src, cache_control=MEDIA_CACHE)


def original(req: Request) -> FileBody | Stream | Json:
    """The original file, or an upright re-encode for a photo stored sideways.

    ``?play=1`` asks for something the window can actually play instead of the
    file itself, for the videos whose format it has no reader for. ``?t=`` is
    where to start: a stream cannot be rewound, so the viewer seeks by asking
    for a new one from a new offset.
    """
    fid = int(req.path.rsplit("/", 1)[1])
    db_path, cache_dir = _open_db_and_cache(req)
    info = browse.media_source(db_path, fid) if db_path else None
    if info is None:
        return NOT_FOUND
    # See thumb() above: info non-None proves cache_dir is a str too.
    cache_dir = cast(str, cache_dir)
    src, sha256, rotate = info
    if req.one("play"):
        chunks = transcode.stream(src, max(0.0, req.one("t", float, 0.0)))
        # No ffmpeg on this machine. The item payload already told the viewer
        # as much (``can_reencode``), so it does not normally ask -- this is
        # the window between the two calls, and the 404 lands it on the same
        # panel it would have shown without asking.
        if chunks is None:
            return Json({"error": "cannot re-encode"}, 404)
        return Stream(chunks, transcode.CONTENT_TYPE)
    # A photo stored sideways is served from an upright re-encode; every
    # other file is served as its own untouched bytes.
    up = thumbnails.upright_for(cache_dir, fid, src, rotate, sha256=sha256)
    return FileBody(up if up else src, cache_control=MEDIA_CACHE)
