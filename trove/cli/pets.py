"""The `trove pets` command: detect animals locally and group pet identities."""

from __future__ import annotations

import argparse
from pathlib import Path

from ..config import Config
from ..db import database as db
from .progress import ScanProgress


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    sp = sub.add_parser("pets", help="Detect animals locally and group likely pet identities")
    sp.add_argument(
        "--limit", type=int, default=None, help="Only scan this many pending images this run"
    )
    sp.add_argument(
        "--recluster", action="store_true", help="Skip detection and rebuild pet identity groups"
    )
    sp.add_argument("--no-progress", action="store_true", help="Disable progress bar")
    sp.set_defaults(func=run)


def run(args: argparse.Namespace, cfg: Config) -> int:
    from ..pets import backend
    from ..pets import cluster as pc
    from ..pets import extract as px

    if not Path(cfg.db_path).exists():
        print("No database yet. Run:  trove init  then  trove scan  then  trove dedup")
        return 1
    if not backend.available():
        print("Pet detection needs OpenCV DNN and NumPy. Install the 'faces' extra and retry.")
        return 1
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    if not args.recluster:
        pending = px.pending_count(conn, model_source=px.scan_source(cfg))
        if pending:
            if not backend.models_ready(cfg.cache_dir):
                print("Fetching the local pet detector once into the model cache …")
            progress = (
                None if args.no_progress else ScanProgress(None, show_bytes=False, label="pets")
            )
            stats = px.extract(conn, cfg, progress=progress, limit=args.limit)
            if progress is not None:
                progress.close()
            print(f"\n  images scanned    : {stats.processed:,}")
            print(f"  animals detected  : {stats.animals:,}")
            print(f"  photos with pets  : {stats.photos_with_animals:,}")
            print(f"  faces suppressed  : {stats.faces_suppressed:,}")
            if stats.errors:
                print(f"  errors            : {stats.errors:,}")
        else:
            print("All canonical images already pet-scanned.")
    print("\nGrouping likely pet identities …")
    grouped = pc.cluster_pets(conn, cfg)
    conn.close()
    print(f"  pet groups        : {grouped.pets:,}")
    print(f"  detections grouped: {grouped.clustered:,}")
    print(f"  unassigned        : {grouped.unassigned:,}")
    return 0
