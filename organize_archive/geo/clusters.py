"""Geographic place clustering: group geolocated files within ``radius_m`` of
each other into one nameable place. Fully idempotent: a run rebuilds a
root's clusters from scratch, carrying a previously-set name over onto
whichever new cluster shares the most members with it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..db import database as db

_EARTH_R = 6_371_000.0  # meters


@dataclass
class ClusterStats:
    clusters: int = 0
    points: int = 0
    named: int = 0


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * _EARTH_R * math.asin(math.sqrt(a))


class _DSU:
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _bucket_and_union(points: list[tuple[float, float]], radius_m: float) -> _DSU:
    """Union points within radius_m using a grid sized to the radius, so
    this stays fast for tens of thousands of points (and for dense
    hotspots -- hundreds of photos taken in one room)."""
    dsu = _DSU(len(points))
    dlat = radius_m / 111_320.0
    keys: list[tuple[int, int]] = []
    cells: dict[tuple[int, int], list[int]] = {}
    for i, (lat, lon) in enumerate(points):
        dlon = radius_m / (111_320.0 * max(math.cos(math.radians(lat)), 1e-6))
        key = (round(lat / dlat), round(lon / dlon))
        keys.append(key)
        cells.setdefault(key, []).append(i)

    # Points sharing a cell are within ~radius_m of each other by
    # construction -- merge them directly, no per-pair distance check.
    for idxs in cells.values():
        for j in idxs[1:]:
            dsu.union(idxs[0], j)

    # Only pairs straddling a cell boundary need the precise haversine check.
    for i, (lat, lon) in enumerate(points):
        cy, cx = keys[i]
        for ny in (cy - 1, cy, cy + 1):
            for nx in (cx - 1, cx, cx + 1):
                if (ny, nx) == (cy, cx):
                    continue
                for j in cells.get((ny, nx), ()):
                    if j <= i:
                        continue
                    lat2, lon2 = points[j]
                    if _haversine_m(lat, lon, lat2, lon2) <= radius_m:
                        dsu.union(i, j)
    return dsu


def cluster_places(conn, root_id: int, radius_m: float = 300.0) -> ClusterStats:
    """Rebuild place clusters for one root. Deletes and recomputes every
    time (like dedup/exact.py's exact-group rebuild) so it stays correct as
    new geo data lands; names are preserved by matching new clusters back to
    old ones by member overlap."""
    db.init_db(conn)
    stats = ClusterStats()
    now = db.now_iso()

    rows = conn.execute(
        """SELECT f.id, g.lat, g.lon FROM files f JOIN geo g ON g.file_id=f.id
           WHERE f.present=1 AND f.root_id=? AND g.lat IS NOT NULL""",
        (root_id,)).fetchall()
    stats.points = len(rows)

    old_by_id: dict[int, tuple[str, set]] = {}
    for r in conn.execute(
        """SELECT pc.id cid, pc.name name, pcm.file_id fid
           FROM place_clusters pc JOIN place_cluster_members pcm ON pcm.cluster_id=pc.id
           WHERE pc.root_id=? AND pc.name IS NOT NULL
           ORDER BY pc.id""", (root_id,)):
        _, fids = old_by_id.setdefault(r["cid"], (r["name"], set()))
        fids.add(r["fid"])
    old_named = list(old_by_id.values())

    conn.execute("DELETE FROM place_clusters WHERE root_id=?", (root_id,))

    if not rows:
        conn.commit()
        return stats

    file_ids = [r["id"] for r in rows]
    points = [(r["lat"], r["lon"]) for r in rows]
    dsu = _bucket_and_union(points, radius_m)

    groups: dict[int, list[int]] = {}
    for i in range(len(points)):
        groups.setdefault(dsu.find(i), []).append(i)

    member_rows = []
    for idxs in groups.values():
        members = [file_ids[i] for i in idxs]
        lat = sum(points[i][0] for i in idxs) / len(idxs)
        lon = sum(points[i][1] for i in idxs) / len(idxs)
        member_set = set(members)
        name, best_overlap = None, 0
        for cand_name, cand_fids in old_named:
            overlap = len(member_set & cand_fids)
            if overlap > best_overlap:
                best_overlap, name = overlap, cand_name
        cur = conn.execute(
            """INSERT INTO place_clusters(root_id, name, lat, lon, member_count, created_at)
               VALUES(?,?,?,?,?,?)""",
            (root_id, name, lat, lon, len(members), now))
        cid = cur.lastrowid
        member_rows.extend((cid, fid) for fid in members)
        stats.clusters += 1
        if name:
            stats.named += 1

    conn.executemany(
        "INSERT INTO place_cluster_members(cluster_id, file_id) VALUES(?,?)", member_rows)
    conn.commit()
    return stats
