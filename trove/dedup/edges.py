"""The near-duplicate pairs themselves, stored so they are found only once.

Duplicate *groups* are still rebuilt wholesale on every run -- that is what
keeps the pass self-healing, and it costs seconds. What did not cost seconds
was rediscovering, from scratch, which fingerprints are close to which: on this
project's own 88,000-photo archive that search was 1,165 of the run's 1,168
seconds, repeated in full because twenty files had been added or five deleted.

So the search is what gets cached. That two files are within
``phash_hamming_threshold`` bits of each other is a fact about their *content*,
and content is exactly what ``files.sha256`` identifies, so the fact stays true
until one of the two files changes. ``dup_edge_scan`` records which content
each file was searched for and under which threshold; either drifting from what
the catalogue and config say now makes that one file's search owed again, and
nothing else's.

The result is that adding twenty files searches for twenty files, deleting
files searches for nothing at all, and the archive still ends up grouped
exactly as a from-scratch run would group it.
"""

from __future__ import annotations

import sqlite3

from ..progress import Progress
from .bands import BandIndex

# Looking one fingerprint up is sub-millisecond, but the loop is still per file,
# so it commits in batches: an interrupted first pass over a large archive keeps
# what it found rather than starting again.
_COMMIT_EVERY = 500


def _scope(root_id: int | None, alias: str = "f") -> tuple[str, tuple[int, ...]]:
    return ("", ()) if root_id is None else (f" AND {alias}.root_id=?", (root_id,))


def _owed(
    conn: sqlite3.Connection, hashes: dict[int, int], threshold: int, root_id: int | None
) -> list[int]:
    """Fingerprinted files whose stored neighbours no longer describe them.

    Covers all three ways that happens with one comparison: never searched (no
    ``dup_edge_scan`` row, which is every file the first time this runs),
    searched against bytes the file no longer has, or searched under a
    different Hamming threshold than the one configured now.

    The query asks the catalogue which files are owed a search and ``hashes``
    -- the fingerprints this run actually holds -- decides which of those can
    have one, so the set searched and the set searchable are the same set by
    construction. Nothing here needs to know which files get fingerprinted:
    the ones that never do (video, an image Pillow cannot open, an animation
    that is deliberately never fingerprinted) come back from the query every
    run and are dropped by the intersection, at the cost of a few thousand
    integers.
    """
    clause, params = _scope(root_id)
    return [
        row[0]
        for row in conn.execute(
            """SELECT f.id FROM files f LEFT JOIN dup_edge_scan s ON s.file_id=f.id
                WHERE f.present=1 AND f.sha256 IS NOT NULL
                  AND (s.file_id IS NULL OR s.sha256 <> f.sha256 OR s.threshold <> ?)"""
            + clause
            + " ORDER BY f.id",
            (threshold, *params),
        )
        if row[0] in hashes
    ]


def refresh(
    conn: sqlite3.Connection,
    hashes: dict[int, int],
    threshold: int,
    progress: Progress | None = None,
    root_id: int | None = None,
) -> None:
    """Bring the stored pairs up to date, searching only for what changed.

    Does nothing at all when every fingerprint's neighbours are already
    recorded, which is the ordinary case and the reason a run after a small
    scan is quick.

    The index only ever holds ``hashes``, so a run scoped to one root can only
    ever store pairs within that root -- which is what keeps groups from
    crossing archive boundaries (ADR 0006). The corollary is that ``root_id``
    must not vary between runs on one catalogue: a whole-catalogue pass after
    per-root ones would find every file already searched and never look for the
    cross-root pairs it exists to find. Nothing in the product does that (the
    GUI always passes its open archive, and the CLI loops roots), and
    cross-archive groups are forbidden anyway.

    Each file is cleared, re-searched and marked without an intervening commit,
    so the invariant "a file recorded against its current SHA has *all* of its
    pairs stored" holds at every commit boundary. That matters because clearing
    one file's pairs also clears its neighbour's record of the same pair: the
    re-search puts the pair back, and only a batch that got that far is ever
    committed.
    """
    owed = _owed(conn, hashes, threshold, root_id)
    if not owed:
        return
    index = BandIndex(threshold)
    for file_id, value in hashes.items():
        index.add(value, file_id)

    if progress is not None:
        # Re-based before the first lookup, not left holding the fingerprinting
        # pass's final count: `cards._dedup_card_message` reads `done > total`
        # as "the grouping loop has started", and a bar inherited from the
        # previous phase would trip it for as long as it took to reset itself.
        progress.total = len(owed)
        progress.update(0, 0, "")
    for done, file_id in enumerate(owed, 1):
        _search_one(conn, index, hashes[file_id], file_id, threshold)
        # Also the cancellation checkpoint: this is the one loop in the pass
        # whose length grows with the archive, so a job asked to stop has to be
        # able to answer from inside it (see pipeline/job.py's JobProgress,
        # which raises on the cancel event from exactly this call).
        #
        # `current` stays empty deliberately. It is what the card shows in its
        # detail line, and what tells it a *photo* is being fingerprinted --
        # which is a different, slower phase. Empty leaves the card on its
        # accurate flat "Finding duplicates…" with a bar that moves, rather
        # than naming the wrong phase for the length of this one.
        if progress is not None and done % 100 == 0:
            progress.update(done, 0, "")
        if done % _COMMIT_EVERY == 0:
            conn.commit()
    conn.commit()
    if progress is not None:
        progress.update(len(owed), 0, "")


def _search_one(
    conn: sqlite3.Connection, index: BandIndex, value: int, file_id: int, threshold: int
) -> None:
    """Replace one file's stored pairs with what the index says they are now."""
    pairs = [
        (min(file_id, other), max(file_id, other))
        for other in index.within(value)
        if other != file_id
    ]
    conn.execute("DELETE FROM dup_edges WHERE lo_file_id=? OR hi_file_id=?", (file_id, file_id))
    # OR IGNORE because a pair of files that are *both* owed is found twice,
    # once from each end, and the second insert is the same row.
    conn.executemany("INSERT OR IGNORE INTO dup_edges(lo_file_id, hi_file_id) VALUES(?,?)", pairs)
    conn.execute(
        """INSERT INTO dup_edge_scan(file_id, sha256, threshold)
           VALUES(?, (SELECT sha256 FROM files WHERE id=?), ?)
           ON CONFLICT(file_id) DO UPDATE SET sha256=excluded.sha256,
               threshold=excluded.threshold""",
        (file_id, file_id, threshold),
    )


def load(conn: sqlite3.Connection, root_id: int | None) -> list[tuple[int, int]]:
    """Every stored pair whose two files are both still present and hashed.

    Filtered rather than deleted, because a file marked missing is not
    necessarily gone: an unplugged drive comes back, and so should its groups
    without the archive being searched again. Rows only disappear when the
    catalogue row does, by cascade.
    """
    clause_a, params_a = _scope(root_id, "a")
    clause_b, params_b = _scope(root_id, "b")
    return [
        (row[0], row[1])
        for row in conn.execute(
            """SELECT e.lo_file_id, e.hi_file_id FROM dup_edges e
                 JOIN files a ON a.id=e.lo_file_id
                 JOIN files b ON b.id=e.hi_file_id
                WHERE a.present=1 AND b.present=1
                  AND a.sha256 IS NOT NULL AND b.sha256 IS NOT NULL"""
            + clause_a
            + clause_b,
            (*params_a, *params_b),
        )
    ]
