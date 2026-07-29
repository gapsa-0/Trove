"""Duplicate grouping: exact bytes plus visually identical image variants.

Each group keeps one **canonical** copy visible; the rest are marked hidden
(never deleted, always reviewable). Selection is deterministic so re-runs are
stable. Fully idempotent: a run rebuilds every duplicate group from scratch.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..db import database as db


@dataclass
class DedupStats:
    groups: int = 0
    duplicate_files: int = 0  # copies hidden (beyond the canonical)
    reclaimable_bytes: int = 0


def perceptual_available() -> bool:
    """Whether this installation can decode and fingerprint images."""
    try:
        import imagehash
        from PIL import Image
    except ImportError:
        return False
    return True


def _pick_canonical(members):
    """Return the best representative, using a stable, documented ordering."""
    return min(
        members,
        key=lambda m: (
            -(m[1] or 0) * (m[2] or 0),  # highest pixel count
            -(m[3] or 0),  # then least-compressed/largest file
            not m[4],
            not m[5],  # then richer provenance
            m[6] is None,
            m[6] or "",  # then earliest resolved date
            m[7],
            m[0],  # then stable path/id
        ),
    )[0]


class _UnionFind:
    def __init__(self, values):
        self.parent = {v: v for v in values}

    def find(self, value):
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, a, b):
        a, b = self.find(a), self.find(b)
        if a != b:
            self.parent[b] = a


class _BKTree:
    """Small metric tree for finding 64-bit hashes within Hamming distance.

    A pairwise comparison turns a 150k-photo archive into billions of checks.
    The tree narrows each lookup using Hamming distance's triangle inequality.
    """

    def __init__(self):
        self.root = None

    @staticmethod
    def _distance(a, b):
        return (a ^ b).bit_count()

    def add(self, value, file_id):
        node = self.root
        if node is None:
            self.root = [value, file_id, {}]
            return
        while True:
            distance = self._distance(value, node[0])
            child = node[2].get(distance)
            if child is None:
                node[2][distance] = [value, file_id, {}]
                return
            node = child

    def within(self, value, radius):
        found, pending = [], [self.root] if self.root else []
        while pending:
            node = pending.pop()
            distance = self._distance(value, node[0])
            if distance <= radius:
                found.append(node[1])
            pending.extend(
                child
                for edge, child in node[2].items()
                if distance - radius <= edge <= distance + radius
            )
        return found


def _perceptual_hashes(
    conn, cfg, progress=None, root_id: int | None = None
) -> tuple[dict[int, int], int, int]:
    """Compute missing/stale pHashes and return ``file_id -> hash``.

    ImageHash/Pillow are optional so exact duplicate detection continues to work
    in a minimal installation.  Decode failures are skipped; a corrupt or RAW
    file must never prevent the rest of an archive from being grouped.
    """
    try:
        import imagehash
        from PIL import Image, ImageOps

        try:
            import pillow_heif

            pillow_heif.register_heif_opener()
        except ImportError:
            pass
    except ImportError:
        return {}, 0, 0

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
    hashes, computed, errors = {}, 0, 0
    for i, row in enumerate(rows, 1):
        raw = row["hash"]
        if row["source_sha256"] == row["sha256"] and raw:
            try:
                hashes[row["id"]] = int(raw, 16)
                continue
            except ValueError:
                pass
        try:
            with Image.open(Path(row["root"]) / row["rel_path"]) as image:
                # Apply EXIF orientation before hashing, so a rotated export and
                # its original compare as the same photograph.
                digest = imagehash.phash(ImageOps.exif_transpose(image))
            value = int(str(digest), 16)
            conn.execute(
                """INSERT INTO perceptual_hashes(file_id, source_sha256, algorithm, hash, created_at)
                   VALUES(?, ?, 'phash64', ?, ?)
                   ON CONFLICT(file_id) DO UPDATE SET source_sha256=excluded.source_sha256,
                       algorithm=excluded.algorithm, hash=excluded.hash, created_at=excluded.created_at""",
                (row["id"], row["sha256"], f"{value:016x}", db.now_iso()),
            )
            hashes[row["id"]] = value
            computed += 1
        except Exception:
            # Corrupt/malformed images throw all sorts of things from deep
            # inside Pillow decoders (struct.error on a bad TIFF/EXIF offset,
            # zlib errors, etc.), not just OSError/ValueError. One bad file
            # must never abort the whole dedup run -- skip and keep going.
            errors += 1
        if progress is not None and i % 100 == 0:
            progress.update(i, 0, row["rel_path"])
        if i % 100 == 0:
            conn.commit()
    conn.commit()
    if progress is not None:
        progress.update(len(rows), 0, "")
    return hashes, computed, errors


def run(conn, cfg=None, progress=None, root_id: int | None = None) -> DedupStats:
    """Rebuild duplicate groups for one archive.

    ``root_id=None`` remains available for callers that explicitly want a
    whole-catalog pass, but the GUI always supplies its open archive.  Groups
    never cross archive boundaries in normal operation.
    """
    db.init_db(conn)
    stats = DedupStats()
    now = db.now_iso()

    # Load all candidates once.  Exact and visual matches are combined with a
    # union-find, ensuring each file belongs to at most one final group.
    root_clause = "" if root_id is None else " AND f.root_id=?"
    root_params = () if root_id is None else (root_id,)
    rows = conn.execute(
        """SELECT f.sha256 sha, f.id, f.size, f.rel_path,
                  (t.file_id IS NOT NULL) has_side, (d.file_id IS NOT NULL) has_date
                  , d.best_datetime, mm.width, mm.height
           FROM files f
           LEFT JOIN takeout_sidecar t ON t.file_id=f.id
           LEFT JOIN dates d ON d.file_id=f.id
           LEFT JOIN media_meta mm ON mm.file_id=f.id
           WHERE f.present=1 AND f.sha256 IS NOT NULL"""
        + root_clause
        + " ORDER BY f.id",
        root_params,
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    uf = _UnionFind(by_id)
    by_sha: dict[str, list[int]] = {}
    for r in rows:
        by_sha.setdefault(r["sha"], []).append(r["id"])
    for ids in by_sha.values():
        for fid in ids[1:]:
            uf.union(ids[0], fid)

    # The perceptual pass is intentionally opt-in through the existing media
    # extra.  It compares all image representations, including exact-group
    # representatives, so JPG/PNG/HEIC exports end up in the same group.
    if cfg is not None:
        hashes, _, _ = _perceptual_hashes(conn, cfg, progress, root_id=root_id)
        threshold = cfg.phash_hamming_threshold
        tree = _BKTree()
        for fid in sorted(hashes):
            value = hashes[fid]
            for other in tree.within(value, threshold):
                uf.union(fid, other)
            tree.add(value, fid)

    # Do not clear the previous, safe grouping until visual fingerprints have
    # been computed.  That way an interrupted pHash pass never exposes a large
    # backlog of duplicate copies to downstream jobs such as face extraction.
    # Older versions grouped the whole catalog. If such a group touches this
    # archive, discard the whole group: retaining it would keep files in a
    # different archive hidden because of a cross-archive match.
    #
    # From here to the final commit below is deliberately ONE transaction: no
    # commit lands between clearing the old groups and writing the new ones,
    # so a crash or a cancelled run partway through regrouping leaves the
    # previous grouping fully intact (rolled back with everything else this
    # connection hasn't committed) instead of publishing a window where every
    # reader sees zero duplicates -- the People/pets backlog inflating and the
    # Overview's duplicate-count tile dropping to zero for the whole run.
    group_clause = (
        ""
        if root_id is None
        else (
            " WHERE id IN (SELECT DISTINCT m.group_id FROM dup_members m "
            "JOIN files f ON f.id=m.file_id WHERE f.root_id=?)"
        )
    )
    group_params = () if root_id is None else (root_id,)
    old_groups = conn.execute("SELECT id FROM dup_groups" + group_clause, group_params).fetchall()
    if old_groups:
        ids = [row[0] for row in old_groups]
        marks = ",".join("?" for _ in ids)
        conn.execute(
            f"UPDATE files SET hidden=0, dup_group_id=NULL WHERE dup_group_id IN ({marks})",
            ids,
        )
        conn.execute(f"DELETE FROM dup_groups WHERE id IN ({marks})", ids)

    buckets: dict[int, list] = {}
    for r in rows:
        buckets.setdefault(uf.find(r["id"]), []).append(r)
    buckets = {root: members for root, members in buckets.items() if len(members) > 1}
    if progress is not None:
        progress.total = len(buckets)

    member_rows, canon_updates, dup_updates = [], [], []
    done = 0
    for members in buckets.values():
        count = len(members)
        packed = [
            (
                m["id"],
                m["width"],
                m["height"],
                m["size"],
                m["has_side"],
                m["has_date"],
                m["best_datetime"],
                m["rel_path"],
            )
            for m in members
        ]
        canon = _pick_canonical(packed)
        canonical_size = by_id[canon]["size"]
        redundant = sum(m["size"] for m in members if m["id"] != canon)
        method = "exact" if len({m["sha"] for m in members}) == 1 else "perceptual"
        cur = conn.execute(
            """INSERT INTO dup_groups(method, canonical_file_id, member_count,
               size_each, redundant_bytes, created_at)
               VALUES(?,?,?,?,?,?)""",
            (method, canon, count, canonical_size, redundant, now),
        )
        gid = cur.lastrowid
        for m in members:
            fid = m["id"]
            role = "canonical" if fid == canon else "duplicate"
            member_rows.append((gid, fid, role))
            (canon_updates if fid == canon else dup_updates).append((gid, fid))

        stats.groups += 1
        stats.duplicate_files += count - 1
        stats.reclaimable_bytes += redundant
        done += 1
        if progress is not None and done % 100 == 0:
            progress.update(done, 0, f"{count}× {method}")

    conn.executemany("INSERT INTO dup_members(group_id, file_id, role) VALUES(?,?,?)", member_rows)
    conn.executemany("UPDATE files SET dup_group_id=?, hidden=0 WHERE id=?", canon_updates)
    conn.executemany("UPDATE files SET dup_group_id=?, hidden=1 WHERE id=?", dup_updates)
    conn.commit()
    if progress is not None:
        progress.update(done, 0, "")
    return stats
