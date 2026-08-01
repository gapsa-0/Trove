"""Thumbnails and the original file, served by bare id (no ``root`` query
param) except for the start-page cover mosaic, which names its archive."""

from __future__ import annotations

from ... import thumbnails
from ...services import browse, people, pets
from ._request import NOT_FOUND, FileBody, Json, Request


# -- media serving ----------------------------------------------------
# Thumbnails and originals are requested by bare id with no ``root``
# (the frontend never sends one for these), so they resolve against
# whichever single archive is currently open, the GUI never browses two
# archives' content at once.
def _open_db_and_cache(req: Request):
    rid = req.open_root_id
    if rid is None:
        return None, None
    return req.db(rid), req.cache(rid)


def thumb(req: Request):
    """A file's thumbnail, generated and cached on first request."""
    fid = int(req.path.rsplit("/", 1)[1])
    db_path, cache_dir = _open_db_and_cache(req)
    info = browse.media_source(db_path, fid) if db_path else None
    if info is None:
        return NOT_FOUND
    src, sha256, rotate = info
    tp = thumbnails.thumb_for(cache_dir, fid, src, sha256=sha256, rotate=rotate)
    return FileBody(tp if tp else src)


def archive_thumb(req: Request):
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
    return FileBody(tp if tp else src)


def face_thumb(req: Request):
    """A cropped face thumbnail, cut from the source photo or (for a video
    detection) its re-derived keyframe."""
    face_id = int(req.path.rsplit("/", 1)[1])
    db_path, cache_dir = _open_db_and_cache(req)
    info = people.face_crop_source(db_path, face_id) if db_path else None
    if info is None:
        return NOT_FOUND
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
        return FileBody(tp if tp else frame)
    tp = thumbnails.face_thumb_for(cache_dir, face_id, src, box, sha256=sha256, rotate=rotate)
    return FileBody(tp if tp else src)


def animal_thumb(req: Request):
    """A cropped pet/animal thumbnail, cut from the source photo or (for a video
    detection) its re-derived keyframe."""
    detection_id = int(req.path.rsplit("/", 1)[1])
    db_path, cache_dir = _open_db_and_cache(req)
    info = pets.animal_crop_source(db_path, detection_id) if db_path else None
    if info is None:
        return NOT_FOUND
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
        return FileBody(tp if tp else frame)
    tp = thumbnails.face_thumb_for(cache_dir, detection_id, src, box, sha256=sha256, rotate=rotate)
    return FileBody(tp if tp else src)


def original(req: Request):
    """The original file, or an upright re-encode for a photo stored sideways."""
    fid = int(req.path.rsplit("/", 1)[1])
    db_path, cache_dir = _open_db_and_cache(req)
    info = browse.media_source(db_path, fid) if db_path else None
    if info is None:
        return NOT_FOUND
    src, sha256, rotate = info
    # A photo stored sideways is served from an upright re-encode; every
    # other file is served as its own untouched bytes.
    up = thumbnails.upright_for(cache_dir, fid, src, rotate, sha256=sha256)
    return FileBody(up if up else src)
