"""Exact deduplication: group byte-identical files by SHA-256.

Each group keeps one **canonical** copy visible; the rest are marked hidden
(never deleted, always reviewable). Selection is deterministic so re-runs are
stable. Fully idempotent: a run rebuilds the 'exact' groups from scratch.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..db import database as db


@dataclass
class DedupStats:
    groups: int = 0
    duplicate_files: int = 0      # copies hidden (beyond the canonical)
    reclaimable_bytes: int = 0


def _pick_canonical(members):
    """Choose the canonical among byte-identical members. Bytes are identical,
    so size/resolution tie; prefer richest provenance then a stable order:
      1. has a Takeout sidecar, 2. has a resolved date,
      3. shortest relative path, 4. lowest file id.
    Each member is (id, plen, has_side, has_date)."""
    return min(members, key=lambda m: (not m[2], not m[3], m[1], m[0]))[0]


def run(conn, progress=None) -> DedupStats:
    db.init_db(conn)
    stats = DedupStats()
    now = db.now_iso()

    # Reset any prior exact-dedup state so the run is idempotent.
    if conn.execute("SELECT 1 FROM dup_groups WHERE method='exact' LIMIT 1").fetchone():
        conn.execute(
            "UPDATE files SET hidden=0, dup_group_id=NULL WHERE dup_group_id IN "
            "(SELECT id FROM dup_groups WHERE method='exact')")
        conn.execute("DELETE FROM dup_groups WHERE method='exact'")
        conn.commit()

    # One bulk pass: all members of duplicated shas, ordered so groups are
    # contiguous. LEFT JOINs (not correlated subqueries) keep it fast.
    rows = conn.execute(
        """SELECT f.sha256 sha, f.id, f.size, length(f.rel_path) plen,
                  (t.file_id IS NOT NULL) has_side, (d.file_id IS NOT NULL) has_date
           FROM files f
           LEFT JOIN takeout_sidecar t ON t.file_id=f.id
           LEFT JOIN dates d ON d.file_id=f.id
           WHERE f.present=1 AND f.sha256 IN (
               SELECT sha256 FROM files WHERE present=1 AND sha256 IS NOT NULL
               GROUP BY sha256 HAVING COUNT(*)>1)
           ORDER BY f.sha256""").fetchall()

    # Bucket rows by sha (contiguous thanks to ORDER BY).
    buckets: dict[str, list] = {}
    for r in rows:
        buckets.setdefault(r["sha"], []).append(
            (r["id"], r["plen"], r["has_side"], r["has_date"], r["size"]))

    if progress is not None:
        progress.total = len(buckets)

    group_rows, member_rows, canon_updates, dup_updates = [], [], [], []
    done = 0
    for sha, members in buckets.items():
        count = len(members)
        size_each = min(m[4] for m in members)
        canon = _pick_canonical(members)
        redundant = (count - 1) * (size_each or 0)
        cur = conn.execute(
            """INSERT INTO dup_groups(method, canonical_file_id, member_count,
               size_each, redundant_bytes, created_at)
               VALUES('exact',?,?,?,?,?)""",
            (canon, count, size_each, redundant, now))
        gid = cur.lastrowid
        for m in members:
            fid = m[0]
            role = "canonical" if fid == canon else "duplicate"
            member_rows.append((gid, fid, role))
            (canon_updates if fid == canon else dup_updates).append((gid, fid))

        stats.groups += 1
        stats.duplicate_files += count - 1
        stats.reclaimable_bytes += redundant
        done += 1
        if progress is not None and done % 100 == 0:
            progress.update(done, 0, f"{count}× {sha[:12]}")

    conn.executemany(
        "INSERT INTO dup_members(group_id, file_id, role) VALUES(?,?,?)", member_rows)
    conn.executemany(
        "UPDATE files SET dup_group_id=?, hidden=0 WHERE id=?", canon_updates)
    conn.executemany(
        "UPDATE files SET dup_group_id=?, hidden=1 WHERE id=?", dup_updates)
    conn.commit()
    if progress is not None:
        progress.update(done, 0, "")
    return stats
