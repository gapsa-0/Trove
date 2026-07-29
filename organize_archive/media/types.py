"""Media-type classification by file extension."""

from __future__ import annotations

IMAGE_EXTS = {
    "jpg",
    "jpeg",
    "png",
    "gif",
    "webp",
    "bmp",
    "tif",
    "tiff",
    "heic",
    "heif",
    "jfif",
    "raw",
    "cr2",
    "nef",
    "arw",
    "dng",
}
VIDEO_EXTS = {
    "mp4",
    "mov",
    "avi",
    "wmv",
    "mkv",
    "3gp",
    "3g2",
    "m4v",
    "mpg",
    "mpeg",
    "flv",
    "webm",
    "mts",
    "m2ts",
    "swf",
}
AUDIO_EXTS = {
    "opus",
    "mp3",
    "m4a",
    "aac",
    "wav",
    "flac",
    "ogg",
    "amr",
    "wma",
}
DOCUMENT_EXTS = {
    "pdf",
    "doc",
    "docx",
    "xls",
    "xlsx",
    "ppt",
    "pptx",
    "odt",
    "ods",
    "odp",
    "txt",
    "csv",
    "html",
    "rtf",
    "ipynb",
    "md",
    "mhtml",
}
ARCHIVE_EXTS = {"zip", "rar", "7z", "tar", "gz", "apk"}


def media_type(ext: str) -> str:
    """Return one of: image, video, audio, document, archive, other."""
    e = ext.lower().lstrip(".")
    if e in IMAGE_EXTS:
        return "image"
    if e in VIDEO_EXTS:
        return "video"
    if e in AUDIO_EXTS:
        return "audio"
    if e in DOCUMENT_EXTS:
        return "document"
    if e in ARCHIVE_EXTS:
        return "archive"
    return "other"
