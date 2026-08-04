"""The `trove enrich` command: resolve dates, GPS and metadata."""

from __future__ import annotations

import argparse
from pathlib import Path

from ..config import Config
from ..db import database as db
from .progress import ScanProgress


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    sp = sub.add_parser("enrich", help="Resolve dates, GPS and metadata (resumable)")
    sp.add_argument("--no-progress", action="store_true", help="Disable progress bar")
    sp.set_defaults(func=run)


def run(args: argparse.Namespace, cfg: Config) -> int:
    from ..metadata import enrich as enrich_mod
    from ..metadata.exiftool_reader import available as exif_available

    if not Path(cfg.db_path).exists():
        print("No database yet. Run:  trove init  then  trove scan")
        return 1

    if not exif_available():
        print(
            "Note: exiftool not found; resolving dates from Takeout JSON, "
            "filenames and file times only (no EXIF)."
        )
    if cfg.timezone is None:
        print(
            "Note: no timezone set; Takeout (UTC) dates may shift evening "
            "photos by a day. Set one with:  trove config --set-timezone <IANA>"
        )

    conn = db.connect(cfg.db_path)
    db.init_db(conn)

    progress = None if args.no_progress else ScanProgress(None, show_bytes=False, label="enriching")
    stats = enrich_mod.enrich(conn, cfg, progress=progress)
    if progress is not None:
        progress.close()
    conn.close()

    print("\nEnrichment complete:")
    print(f"  files processed   : {stats.processed}")
    print(f"  matched Takeout   : {stats.with_takeout}")
    print(f"  with GPS location : {stats.with_gps}")
    print("  date source used  :")
    for src, n in sorted(stats.by_source.items(), key=lambda x: -x[1]):
        print(f"      {src:<14} {n}")
    return 0
