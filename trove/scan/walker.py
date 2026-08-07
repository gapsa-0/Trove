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

# How recently a file must have been written to be treated as still arriving.
#
# Short on purpose, and the shortness is the point rather than a compromise. A
# copy in progress has its mtime rewritten continuously, so it reads as zero
# seconds old however long it runs; a photograph that has finished landing ages
# past this within a moment. One second separates those two cleanly, and every
# tenth of a second beyond it is dead time in front of a folder someone has just
# dropped pictures into -- which is the case this whole path exists to make
# fast. Widening it does not make the check safer, because it is not what makes
# it correct: `_still_arriving` does, after the read, where no window can be
# wrong.
SETTLE_SECONDS = 1.0


@dataclass
class ScanStats:
    seen: int = 0
    new: int = 0
    updated: int = 0
    skipped: int = 0
    ignored: int = 0
    errors: int = 0
    bytes_hashed: int = 0
    # Files that were being written while this run walked past them. They are
    # left for a later pass rather than catalogued half-copied, and the count
    # travels as far as scan_runs.files_unstable, because a run that skipped
    # one did not cover the tree and must not be recorded as though it had.
    unstable: int = 0
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


def _arriving_now(st: os.stat_result) -> bool:
    """Whether this file was written so recently that it is probably still being
    written to.

    ``abs`` rather than "younger than now", because a future timestamp is not
    evidence of an arrival in progress: a camera with the wrong date writes
    files stamped years ahead, and treating those as forever-in-flight would
    mean never cataloguing them at all. A copy in progress sits within a second
    or two of now on either side, which is what this is looking for -- coarse
    filesystem timestamps (FAT and its descendants round to two seconds) can
    otherwise put a file that was just written slightly in the future.
    """
    return abs(time.time() - st.st_mtime) < SETTLE_SECONDS


def _still_arriving(path: Path, before: os.stat_result) -> bool:
    """Whether the file moved under us while we were reading it.

    The half that catches a large video. Waiting beforehand cannot: a 40 GB copy
    takes longer than any window worth blocking a scan for, and by the time
    hashing starts the file looks as settled as it ever will. Reading it and
    then asking whether it is still the same file costs one stat and needs no
    window at all -- if size or mtime moved, the hash is of bytes that no longer
    describe anything, and the file is left for the next pass.

    A file that vanished mid-read counts as still arriving: there is nothing to
    record either way, and a run that saw one has not covered the tree.
    """
    try:
        after = path.stat()
    except OSError:
        return True
    return after.st_size != before.st_size or abs(after.st_mtime - before.st_mtime) > 1e-6


def _mark_present(conn: sqlite3.Connection, now: str, existing: sqlite3.Row | None) -> None:
    """Keep a row this run is not going to rewrite from being marked missing.

    ``scan_root`` finishes by setting ``present=0`` on every row it did not
    touch, so a file skipped for any reason still needs its ``last_seen``
    bumped. Without this, re-copying a file over one already in the catalogue
    would make it disappear from the library for as long as the copy took.
    """
    if existing is not None:
        conn.execute("UPDATE files SET last_seen=?, present=1 WHERE id=?", (now, existing["id"]))


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
    count_seen: bool = True,
) -> bool:
    """Catalogue one file. Raises OSError if it cannot be read; the caller counts
    that as an error and moves on rather than failing the whole scan.

    Returns True when the file was left alone because it is still arriving. The
    caller keeps those and comes back to them at the end of the walk rather than
    counting them lost -- ``count_seen`` is False on that second visit, so a file
    looked at twice is not counted twice.
    """
    st = path.stat()
    if count_seen:
        stats.seen += 1
    rel = str(path.relative_to(root))
    existing = conn.execute(
        "SELECT id, size, mtime, sha256 FROM files WHERE root_id=? AND rel_path=?",
        (root_id, rel),
    ).fetchone()
    if _unchanged(existing, st.st_size, st.st_mtime):
        conn.execute("UPDATE files SET last_seen=?, present=1 WHERE id=?", (now, existing["id"]))
        stats.skipped += 1
        return False
    # Do not read a file that is still arriving. What would be stored is a hash
    # of half a video and the size it had reached, and everything derived from
    # those bytes -- thumbnail, dates, faces, embedding -- would be derived from
    # a fragment and then thrown away when the copy finished.
    if _arriving_now(st):
        _mark_present(conn, now, existing)
        return True
    fh = hasher.fast_hash(path, st.st_size, cfg.fast_hash_sample_bytes)
    sh = hasher.sha256(path)
    if _still_arriving(path, st):
        _mark_present(conn, now, existing)
        return True
    stats.bytes_hashed += st.st_size
    _write_file_row(conn, root_id, rel, path.name, st, (fh, sh), now, existing, stats)
    return False


# At most this many still-arriving files are revisited at the end of a run.
# The list is only ever as long as what was landing during the walk; the cap is
# there so that pointing an archive at a folder another program is actively
# filling cannot turn the retry into a second full pass.
_MAX_REVISITS = 5000


def _revisit_arrivals(
    conn: sqlite3.Connection,
    cfg: Config,
    root_id: int,
    root: Path,
    arriving: list[Path],
    now: str,
    stats: ScanStats,
) -> None:
    """Look once more at the files that were being written as we passed them.

    Without this, a photograph copied in a moment before the scan reached it
    would wait for the *next* scan -- and the next scan is held off for
    ``stages.SETTLE_RECHECK`` precisely so that a large video does not cost a
    walk every few seconds. So the case this whole path exists to make fast
    would have become the slowest one in it.

    Coming back at the end of the walk costs a stat and, for anything that has
    landed, the read it was always going to need. Nothing is waited for beyond
    the settle window itself, and on any archive big enough for the walk to take
    longer than that, nothing is waited for at all. What is still growing on the
    second look -- the large video -- stays unstable and is left to a later run,
    which is the outcome the recheck delay is sized for.
    """
    quiet_at = min(p.stat().st_mtime for p in arriving if p.exists()) if arriving else 0.0
    wait = SETTLE_SECONDS - (time.time() - quiet_at)
    if wait > 0:
        time.sleep(min(wait, SETTLE_SECONDS))
    for path in arriving:
        try:
            if _scan_one(conn, cfg, root_id, root, path, now, stats, count_seen=False):
                stats.unstable += 1
        except OSError:
            # Gone or unreadable on the second look. It was counted as seen on
            # the first pass and there is nothing to record, so the run simply
            # did not cover it.
            stats.unstable += 1


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

    # Files that were being written as the walk went past them. Kept so the run
    # can come back to them at the end instead of leaving them for a later scan
    # -- see _revisit_arrivals. Capped because a folder being filled by another
    # program could otherwise put every file in it on this list; past the cap
    # they are simply left, which is the behaviour without the retry.
    arriving: list[Path] = []
    try:
        for path in iter_files(root):
            if is_ignored(path.name):
                stats.ignored += 1
                continue
            try:
                if _scan_one(conn, cfg, root_id, root, path, now, stats):
                    if len(arriving) < _MAX_REVISITS:
                        arriving.append(path)
                    else:
                        stats.unstable += 1
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

        if arriving:
            _revisit_arrivals(conn, cfg, root_id, root, arriving, now, stats)
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
