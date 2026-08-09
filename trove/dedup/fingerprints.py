"""Perceptual fingerprints: computing them, and answering from the cache.

One 64-bit ``phash64`` per image, stored in ``perceptual_hashes`` against the
content SHA it was taken from -- so a file whose bytes change is fingerprinted
again automatically, and a file whose bytes did not is never decoded twice.
This is the only part of duplicate detection that opens files.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from ..db import database as db
from ..progress import Progress

logger = logging.getLogger(__name__)


def perceptual_available() -> bool:
    """Whether this installation can decode and fingerprint images."""
    # Importing is the probe, so the names go unused on purpose. Deliberately not
    # importlib.util.find_spec: that only proves a module is on the path, and a
    # half-installed native extension would pass it and then fail at real use.
    try:
        import imagehash  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        return False
    return True


def _imaging_ready() -> bool:
    """Whether this run can fingerprint, with HEIC support registered if present.

    Probed once before the pass rather than per file, so a minimal installation
    returns an empty result instead of failing 150,000 times over.
    """
    if not perceptual_available():
        return False
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    return True


def _fingerprint(path: Path) -> int | None:
    """One file's 64-bit perceptual hash, or None where it must not have one.

    None for an animated file. Pillow hands back frame 0 and nothing else, so
    a fingerprint of an animation describes only how it OPENS: two unrelated
    GIFs sharing a title card or a fade-in from white come out identical
    (measured at 0 bits apart) and one is hidden as a copy of the other. Video
    is exact-match only for exactly this reason, and an animated GIF is a video
    wearing an image extension.

    Asked of the decoded image rather than the file name, so a GIF saved as
    ``.png`` is caught by the same rule -- Pillow reads the magic bytes and
    opens it correctly however it is named.

    Imports at call time like its caller: Pillow and ImageHash are optional,
    and ``compute`` has already proved they are importable before anything
    reaches here.
    """
    import imagehash
    from PIL import Image, ImageOps

    with Image.open(path) as image:
        if getattr(image, "n_frames", 1) > 1:
            return None
        # Apply EXIF orientation before hashing, so a rotated export and its
        # original compare as the same photograph.
        return int(str(imagehash.phash(ImageOps.exif_transpose(image))), 16)


def _cached(row: sqlite3.Row) -> int | None:
    """The stored fingerprint, if it was taken from the content the file has now."""
    raw = row["hash"]
    if row["source_sha256"] != row["sha256"] or not raw:
        return None
    try:
        return int(raw, 16)
    except ValueError:
        return None


def compute(
    conn: sqlite3.Connection,
    progress: Progress | None = None,
    root_id: int | None = None,
) -> dict[int, int]:
    """``file_id -> fingerprint`` for every in-scope image, decoding what is missing.

    ImageHash/Pillow are optional so exact duplicate detection continues to work
    in a minimal installation, which is what the empty result means.  Decode
    failures are skipped; a corrupt or RAW file must never prevent the rest of
    an archive from being grouped.

    Animated files are skipped too (see ``_fingerprint``), and never get a
    stored fingerprint -- so they are re-opened on every run rather than
    answered from the cache. That is the cheap side of the trade: counting a
    GIF's frames costs a fraction of the decode-and-DCT every photograph in the
    archive pays for.
    """
    if not _imaging_ready():
        return {}

    root_clause = "" if root_id is None else " AND f.root_id=?"
    params = () if root_id is None else (root_id,)
    rows = conn.execute(
        """SELECT f.id, f.sha256, f.rel_path, r.path root, p.source_sha256, p.hash
           FROM files f JOIN roots r ON r.id=f.root_id
           LEFT JOIN perceptual_hashes p ON p.file_id=f.id AND p.algorithm='phash64'
           WHERE f.present=1 AND f.media_type='image' AND f.sha256 IS NOT NULL"""
        + root_clause
        + " ORDER BY f.id",
        params,
    ).fetchall()
    if progress is not None:
        progress.total = len(rows)
    hashes: dict[int, int] = {}
    for i, row in enumerate(rows, 1):
        cached = _cached(row)
        if cached is not None:
            hashes[row["id"]] = cached
        else:
            _decode_one(conn, row, hashes)
        if progress is not None and i % 100 == 0:
            progress.update(i, 0, row["rel_path"])
        if i % 100 == 0:
            conn.commit()
    conn.commit()
    if progress is not None:
        progress.update(len(rows), 0, "")
    return hashes


def _decode_one(conn: sqlite3.Connection, row: sqlite3.Row, hashes: dict[int, int]) -> None:
    """Fingerprint one file and cache it, or record why it has none."""
    try:
        value = _fingerprint(Path(row["root"]) / row["rel_path"])
    except Exception as exc:
        # Corrupt/malformed images throw all sorts of things from deep inside
        # Pillow decoders (struct.error on a bad TIFF/EXIF offset, zlib errors,
        # etc.), not just OSError/ValueError. One bad file must never abort the
        # whole dedup run -- skip and keep going. No exc_info: this loop runs
        # over ~150k files, a traceback per bad file would flood the rotated log
        # until nothing useful survives.
        logger.warning("phash failed for file_id=%s: %s", row["id"], exc)
        return
    if value is None:  # animated: deliberately never fingerprinted
        return
    conn.execute(
        """INSERT INTO perceptual_hashes(file_id, source_sha256, algorithm, hash, created_at)
           VALUES(?, ?, 'phash64', ?, ?)
           ON CONFLICT(file_id) DO UPDATE SET source_sha256=excluded.source_sha256,
               algorithm=excluded.algorithm, hash=excluded.hash, created_at=excluded.created_at""",
        (row["id"], row["sha256"], f"{value:016x}", db.now_iso()),
    )
    hashes[row["id"]] = value
