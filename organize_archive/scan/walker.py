"""Resumable directory walker + indexer.

Walks each configured root read-only, applies ignore rules, and upserts a row
per media file. Incremental: a file whose (size, mtime) is unchanged and which
is already hashed is skipped without re-reading its content. Safe to interrupt
and re-run — upserts are idempotent and commits happen in batches.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from ..config import (
    IGNORE_EXTENSIONS,
    IGNORE_FILENAMES,
    IGNORE_NAME_SUBSTRINGS,
    Config,
)
from ..db import database as db
from ..hashing import hasher
from ..media.types import media_type
from ..progress import Progress


@dataclass
class ScanStats:
    seen: int = 0
    new: int = 0
    updated: int = 0
    skipped: int = 0
    ignored: int = 0
    errors: int = 0
    bytes_hashed: int = 0
    error_samples: list[str] = field(default_factory=list)


def _ext_of(name: str) -> str:
    dot = name.rfind(".")
    return name[dot + 1 :].lower() if dot > 0 else ""


def is_ignored(name: str) -> bool:
    low = name.lower()
    if low in IGNORE_FILENAMES:
        return True
    if _ext_of(name) in IGNORE_EXTENSIONS:
        return True
    return any(s in low for s in IGNORE_NAME_SUBSTRINGS)


def iter_files(root: Path) -> Iterator[Path]:
    """Yield every regular file under ``root`` (symlinks not followed)."""
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            yield Path(entry.path)
                    except OSError:
                        continue
        except (PermissionError, FileNotFoundError, NotADirectoryError):
            continue


def count_files(root: Path) -> int:
    """Fast pre-count of non-ignored files (scandir only, no hashing)."""
    return sum(1 for p in iter_files(root) if not is_ignored(p.name))


# Everything derived from a file's *content*, in the order they must be dropped
# (nonhuman_detections points at faces, so it goes first). Place membership is
# deliberately absent: it is path-level metadata a user may have set by hand,
# and re-saving the same bytes at the same path should not discard it.
_CONTENT_DERIVED = (
    "nonhuman_detections",
    "animal_detections",
    "pet_scan",
    "faces",
    "face_scan",
    "media_meta",
    "dates",
    "geo",
    "takeout_sidecar",
    "perceptual_hashes",
    "semantic_embeddings",
)


def _clear_derived_rows(conn: sqlite3.Connection, fid: int) -> None:
    """Make every content-derived fact about this file pending again."""
    for table in _CONTENT_DERIVED:
        conn.execute(f"DELETE FROM {table} WHERE file_id=?", (fid,))


def _unchanged(existing: sqlite3.Row | None, size: int, mtime: float) -> bool:
    """True when path, size and mtime all match and a hash is already stored.

    This is the whole incremental story: a re-scan of ~150k files is cheap
    exactly because it answers this from the `files` row and never opens the
    file. The stored-hash check matters for a row written by an interrupted run.
    """
    return bool(
        existing
        and existing["size"] == size
        and abs(existing["mtime"] - mtime) < 1e-6
        and existing["sha256"] is not None
    )


def _write_file_row(
    conn: sqlite3.Connection,
    root_id: int,
    rel: str,
    name: str,
    st: os.stat_result,
    hashes: tuple[str, str],
    now: str,
    existing: sqlite3.Row | None,
    stats: ScanStats,
) -> None:
    """Insert or update one file's row, clearing derived rows if content moved on."""
    fh, sh = hashes
    ext = _ext_of(name)
    mt = media_type(ext)
    size, mtime = st.st_size, st.st_mtime
    if existing:
        if existing["sha256"] != sh:
            # Content at this path changed, so everything derived from the old
            # bytes is now wrong rather than merely stale.
            _clear_derived_rows(conn, existing["id"])
        conn.execute(
            """UPDATE files SET ext=?, size=?, mtime=?, media_type=?,
               fast_hash=?, sha256=?, last_seen=?, present=1 WHERE id=?""",
            (ext, size, mtime, mt, fh, sh, now, existing["id"]),
        )
        stats.updated += 1
    else:
        conn.execute(
            """INSERT INTO files(root_id, rel_path, ext, size, mtime,
               media_type, fast_hash, sha256, first_seen, last_seen, present)
               VALUES(?,?,?,?,?,?,?,?,?,?,1)""",
            (root_id, rel, ext, size, mtime, mt, fh, sh, now, now),
        )
        stats.new += 1


def _scan_one(
    conn: sqlite3.Connection,
    cfg: Config,
    root_id: int,
    root: Path,
    path: Path,
    now: str,
    stats: ScanStats,
) -> None:
    """Catalogue one file. Raises OSError if it cannot be read; the caller counts
    that as an error and moves on rather than failing the whole scan."""
    st = path.stat()
    stats.seen += 1
    rel = str(path.relative_to(root))
    existing = conn.execute(
        "SELECT id, size, mtime, sha256 FROM files WHERE root_id=? AND rel_path=?",
        (root_id, rel),
    ).fetchone()
    if _unchanged(existing, st.st_size, st.st_mtime):
        conn.execute("UPDATE files SET last_seen=?, present=1 WHERE id=?", (now, existing["id"]))
        stats.skipped += 1
        return
    fh = hasher.fast_hash(path, st.st_size, cfg.fast_hash_sample_bytes)
    sh = hasher.sha256(path)
    stats.bytes_hashed += st.st_size
    _write_file_row(conn, root_id, rel, path.name, st, (fh, sh), now, existing, stats)


def scan_root(
    conn: sqlite3.Connection,
    cfg: Config,
    root_path: str,
    run_started: str,
    progress: Progress | None = None,
    commit_every: int = 500,
    base_done: int = 0,
    base_bytes: int = 0,
    root_id: int | None = None,
) -> ScanStats:
    root = Path(root_path)
    if not root.is_dir():
        raise FileNotFoundError(f"Root not found or not a directory: {root_path}")

    # A caller that already knows which root it is scanning (the GUI, where the
    # root id *is* the archive id) says so, rather than having it looked up by
    # path. Resolving by path is what let a mismatched database grow a second
    # root nobody queries — see db.reconcile_root. The shared CLI catalog, which
    # holds several roots and identifies them only by path, keeps the lookup.
    if root_id is None:
        root_id = db.get_or_create_root(conn, str(root))
    stats = ScanStats()
    now = db.now_iso()
    batch = 0
    last_commit = time.monotonic()

    try:
        for path in iter_files(root):
            if is_ignored(path.name):
                stats.ignored += 1
                continue
            try:
                _scan_one(conn, cfg, root_id, root, path, now, stats)
            except OSError as e:
                # One unreadable file must never end the scan: record a sample
                # and carry on, same as a failed stat() always did.
                stats.errors += 1
                if len(stats.error_samples) < 10:
                    stats.error_samples.append(f"{path}: {e}")
                continue

            if progress is not None:
                progress.update(
                    base_done + stats.seen, base_bytes + stats.bytes_hashed, current=path.name
                )

            # Commit by count or by time, so an abrupt kill loses little work.
            batch += 1
            if batch >= commit_every or (time.monotonic() - last_commit) > 2:
                conn.commit()
                batch = 0
                last_commit = time.monotonic()

        conn.commit()
    except KeyboardInterrupt:
        # Save what we have; do NOT mark files missing on a partial scan.
        conn.commit()
        raise

    # Mark files under this root not seen in this run as missing.
    conn.execute(
        "UPDATE files SET present=0 WHERE root_id=? AND last_seen < ?",
        (root_id, run_started),
    )
    conn.commit()
    return stats
