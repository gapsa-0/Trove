"""Duplicate grouping: exact bytes plus visually identical image variants.

Each group keeps one **canonical** copy visible; the rest are marked hidden
(never deleted, always reviewable). Selection is deterministic so re-runs are
stable, and every run rebuilds every duplicate group from scratch -- a
corrupted or half-written grouping is repaired by the next pass rather than
persisting.

What a run does *not* redo from scratch is the search for near-duplicate pairs.
That search is the expensive half by three orders of magnitude, its answers
depend only on file content, and they are cached and invalidated per file in
``edges.py``. The grouping a run produces is identical either way; only the
time taken differs, and it is the difference between twenty minutes and a few
seconds on a 90,000-file archive that gained twenty files.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..config import Config
from ..db import database as db
from ..progress import Progress
from . import edges, fingerprints, groups, keeps
from .bands import UnionFind
from .fingerprints import perceptual_available

__all__ = ["DedupStats", "perceptual_available", "run"]


@dataclass
class DedupStats:
    groups: int = 0
    duplicate_files: int = 0  # copies hidden (beyond the canonical)
    reclaimable_bytes: int = 0


def _relate(
    conn: sqlite3.Connection,
    cfg: Config | None,
    rows: list[sqlite3.Row],
    progress: Progress | None,
    root_id: int | None,
) -> UnionFind:
    """Union files that are the same content: identical sha256, then look-alikes.

    Exact and visual matches go into one union-find, so each file ends up in at
    most one final group. The perceptual half is opt-in through the existing
    media extra; it compares all image representations, including exact-group
    representatives, so JPG/PNG/HEIC exports of one photo end up in the same
    group.

    Passing no config is the exact-only mode: no fingerprints are computed,
    *and* no stored pair is read, so the caller gets byte-identical grouping
    and nothing else.
    """
    uf = UnionFind(r["id"] for r in rows)
    by_sha: dict[str, list[int]] = {}
    for r in rows:
        by_sha.setdefault(r["sha"], []).append(r["id"])
    for ids in by_sha.values():
        for fid in ids[1:]:
            uf.union(ids[0], fid)
    if cfg is None:
        return uf

    hashes = fingerprints.compute(conn, progress, root_id)
    # No fingerprints means the media extra is gone (or the archive holds no
    # images). Stored pairs are still applied: losing an optional dependency
    # should cost the *discovery* of new near-duplicates, not the groups an
    # earlier run already paid to find.
    if hashes:
        edges.refresh(conn, hashes, cfg.phash_hamming_threshold, progress, root_id)
    for a, b in edges.load(conn, root_id):
        if a in uf.parent and b in uf.parent:
            uf.union(a, b)
    return uf


def _components(uf: UnionFind, rows: list[sqlite3.Row]) -> list[list[sqlite3.Row]]:
    """The rows of every group with more than one member."""
    buckets: dict[int, list[sqlite3.Row]] = {}
    for r in rows:
        buckets.setdefault(uf.find(r["id"]), []).append(r)
    return [members for members in buckets.values() if len(members) > 1]


def run(
    conn: sqlite3.Connection,
    cfg: Config | None = None,
    progress: Progress | None = None,
    root_id: int | None = None,
) -> DedupStats:
    """Rebuild duplicate groups for one archive.

    ``root_id=None`` remains available for callers that explicitly want a
    whole-catalog pass, but the GUI always supplies its open archive.  Groups
    never cross archive boundaries in normal operation.
    """
    db.init_db(conn)
    stats = DedupStats()
    now = db.now_iso()

    rows = groups.load_candidates(conn, root_id)
    by_id = {r["id"]: r for r in rows}
    uf = _relate(conn, cfg, rows, progress, root_id)

    # Do not clear the previous, safe grouping until visual fingerprints and
    # their pairs are on disk.  That way an interrupted pass never exposes a
    # large backlog of duplicate copies to downstream jobs such as face
    # extraction.
    #
    # From here to the final commit below is deliberately ONE transaction: no
    # commit lands between clearing the old groups and writing the new ones,
    # so a crash or a cancelled run partway through regrouping leaves the
    # previous grouping fully intact (rolled back with everything else this
    # connection hasn't committed) instead of publishing a window where every
    # reader sees zero duplicates -- the People/pets backlog inflating and the
    # Overview's duplicate-count tile dropping to zero for the whole run.
    groups.clear(conn, root_id)

    found = _components(uf, rows)
    if progress is not None:
        progress.total = len(found)

    member_rows: list[tuple[int, int, str]] = []
    canon_updates: list[tuple[int, int]] = []
    dup_updates: list[tuple[int, int]] = []
    for done, members in enumerate(found, 1):
        written = groups.write(conn, members, by_id, now)
        member_rows.extend(written.members)
        canon_updates.append(written.canonical)
        dup_updates.extend(written.duplicates)
        stats.groups += 1
        stats.duplicate_files += len(members) - 1
        stats.reclaimable_bytes += written.redundant_bytes
        if progress is not None and done % 100 == 0:
            progress.update(done, 0, f"{len(members)}× {written.method}")

    conn.executemany("INSERT INTO dup_members(group_id, file_id, role) VALUES(?,?,?)", member_rows)
    conn.executemany("UPDATE files SET dup_group_id=?, hidden=0 WHERE id=?", canon_updates)
    conn.executemany("UPDATE files SET dup_group_id=?, hidden=1 WHERE id=?", dup_updates)
    # ...and then over the top of that, wherever the user has said which copies
    # they want. The two writes above set the automatic answer for every group
    # including the overridden ones, which keeps this one sweep rather than a
    # branch per group; see dedup/keeps.py.
    keeps.apply(conn)
    conn.commit()
    if progress is not None:
        progress.update(len(found), 0, "")
    return stats
