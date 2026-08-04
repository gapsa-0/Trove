"""The `trove dedup` command: group exact and near-duplicate copies."""

from __future__ import annotations

import argparse
from pathlib import Path

from ..config import Config
from ..db import database as db
from ._common import _fmt_bytes
from .progress import ScanProgress


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    sp = sub.add_parser(
        "dedup", help="Group exact duplicates and visually identical image variants"
    )
    sp.add_argument("--no-progress", action="store_true", help="Disable progress bar")
    sp.set_defaults(func=run)


def run(args: argparse.Namespace, cfg: Config) -> int:
    from ..dedup import exact

    if not Path(cfg.db_path).exists():
        print("No database yet. Run:  trove init  then  trove scan")
        return 1
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    progress = None if args.no_progress else ScanProgress(None, show_bytes=False, label="grouping")
    # Keep archive boundaries intact even when several roots share one catalog.
    # A separate run per root also prevents a copy in one archive from hiding a
    # file in another archive.
    stats = exact.DedupStats()
    roots = conn.execute("SELECT id FROM roots ORDER BY id").fetchall()
    for root in roots:
        one = exact.run(conn, cfg, progress=progress, root_id=root[0])
        stats.groups += one.groups
        stats.duplicate_files += one.duplicate_files
        stats.reclaimable_bytes += one.reclaimable_bytes
    if progress is not None:
        progress.close()
    conn.close()
    print("\nDuplicate detection complete (exact + visual image matches):")
    print(f"  duplicate groups   : {stats.groups:,}")
    print(f"  redundant copies   : {stats.duplicate_files:,} (hidden, not deleted)")
    print(f"  reclaimable space  : {_fmt_bytes(stats.reclaimable_bytes)}")
    if not exact.perceptual_available():
        print(
            "  note: visual matching needs the optional media dependencies "
            "(install with: pip install '.[media]')"
        )
    return 0
