"""Command-line interface for organize_archive.

Runs on the standard library alone. `rich` is used for prettier output when
installed, but the tool degrades gracefully without it.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys

from . import __version__
from .config import Config
from .db import database as db
from .scan import walker
from .scan.progress import ScanProgress


def _preflight() -> list[str]:
    """Return a list of missing-but-recommended system tools."""
    missing = []
    for tool in ("exiftool", "ffprobe"):
        if shutil.which(tool) is None:
            missing.append(tool)
    return missing


def _fmt_bytes(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if f < 1024 or unit == "PB":
            return f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} PB"


# -- commands ---------------------------------------------------------------

def cmd_init(args, cfg: Config) -> int:
    cfg.ensure_dirs()
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    for r in cfg.roots:
        db.get_or_create_root(conn, r)
    conn.close()
    print(f"Initialized database at {cfg.db_path}")
    print(f"Cache directory:        {cfg.cache_dir}")
    print(f"Configured roots:       {', '.join(cfg.roots)}")
    missing = _preflight()
    if missing:
        print(f"\nNote: optional tools not found: {', '.join(missing)} "
              f"(needed for metadata/video in later phases).")
    return 0


def cmd_scan(args, cfg: Config) -> int:
    cfg.ensure_dirs()
    conn = db.connect(cfg.db_path)
    db.init_db(conn)

    roots = args.root or cfg.roots
    run_started = db.now_iso()
    cur = conn.execute(
        "INSERT INTO scan_runs(started_at, roots) VALUES(?, ?)",
        (run_started, json.dumps(roots)),
    )
    run_id = cur.lastrowid
    conn.commit()

    # Pre-count for an accurate progress bar (fast: scandir only, no hashing).
    progress = None
    if not args.no_progress:
        from pathlib import Path
        print("Counting files…", flush=True)
        total = sum(walker.count_files(Path(r)) for r in roots if Path(r).is_dir())
        print(f"  {total} media files to check.")
        progress = ScanProgress(total)

    totals = walker.ScanStats()
    for root in roots:
        print(f"Scanning: {root}")
        try:
            stats = walker.scan_root(
                conn, cfg, root, run_started, progress=progress,
                base_done=totals.seen, base_bytes=totals.bytes_hashed,
            )
        except FileNotFoundError as e:
            print(f"  ! {e}", file=sys.stderr)
            continue
        totals.seen += stats.seen
        totals.new += stats.new
        totals.updated += stats.updated
        totals.skipped += stats.skipped
        totals.ignored += stats.ignored
        totals.errors += stats.errors
        totals.bytes_hashed += stats.bytes_hashed
        totals.error_samples.extend(stats.error_samples[:5])

    if progress is not None:
        progress.close()

    conn.execute(
        """UPDATE scan_runs SET finished_at=?, files_seen=?, files_new=?,
           files_updated=?, bytes_hashed=? WHERE id=?""",
        (db.now_iso(), totals.seen, totals.new, totals.updated,
         totals.bytes_hashed, run_id),
    )
    conn.commit()
    conn.close()

    print("\nScan complete:")
    print(f"  media files seen : {totals.seen}")
    print(f"  new              : {totals.new}")
    print(f"  updated          : {totals.updated}")
    print(f"  unchanged        : {totals.skipped}")
    print(f"  ignored (junk)   : {totals.ignored}")
    print(f"  hashed this run  : {_fmt_bytes(totals.bytes_hashed)}")
    if totals.errors:
        print(f"  errors           : {totals.errors}")
        for s in totals.error_samples:
            print(f"      - {s}")
    return 0


def cmd_status(args, cfg: Config) -> int:
    from pathlib import Path
    if not Path(cfg.db_path).exists():
        print("No database yet. Run:  oa init")
        return 1
    conn = db.open_readonly(cfg.db_path)

    total, present, hashed, missing, size = conn.execute(
        """SELECT COUNT(*),
                  SUM(present=1),
                  SUM(sha256 IS NOT NULL),
                  SUM(present=0),
                  COALESCE(SUM(size), 0)
           FROM files"""
    ).fetchone()

    print(f"organize_archive v{__version__}")
    print(f"Database: {cfg.db_path}")
    print(f"Roots:    {', '.join(cfg.roots)}")
    print()
    print(f"Files indexed : {total or 0}  (present {present or 0}, missing {missing or 0})")
    print(f"Hashed        : {hashed or 0}")
    print(f"Total size    : {_fmt_bytes(size or 0)}")

    print("\nBy media type:")
    rows = conn.execute(
        """SELECT media_type, COUNT(*) c, COALESCE(SUM(size),0) s
           FROM files WHERE present=1 GROUP BY media_type ORDER BY c DESC"""
    ).fetchall()
    for r in rows:
        print(f"  {r['media_type']:<10} {r['c']:>8}   {_fmt_bytes(r['s'])}")

    last = conn.execute(
        "SELECT started_at, finished_at FROM scan_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if last:
        state = last["finished_at"] or "(unfinished)"
        print(f"\nLast scan: {last['started_at']} → {state}")
    conn.close()
    return 0


def cmd_config(args, cfg: Config) -> int:
    if args.show:
        from dataclasses import asdict
        print(json.dumps(asdict(cfg), indent=2))
    if args.add_root:
        if args.add_root not in cfg.roots:
            cfg.roots.append(args.add_root)
            cfg.save()
            print(f"Added root: {args.add_root}")
        else:
            print("Root already configured.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="oa", description=__doc__)
    p.add_argument("--version", action="version", version=f"organize_archive {__version__}")
    p.add_argument("--db", metavar="PATH",
                   help="Use this database file instead of the configured default "
                        "(useful for isolated testing while a scan runs).")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("init", help="Create the database and register roots")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("scan", help="Walk roots and index/hash media files (resumable)")
    sp.add_argument("--root", action="append", help="Scan this root only (repeatable)")
    sp.add_argument("--no-progress", action="store_true",
                    help="Disable the progress bar and pre-count")
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("status", help="Show catalog summary")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("config", help="Show or modify configuration")
    sp.add_argument("--show", action="store_true", help="Print current config")
    sp.add_argument("--add-root", metavar="PATH", help="Add a source root")
    sp.set_defaults(func=cmd_config)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = Config.load()
    if args.db:
        cfg.db_path = args.db
    return args.func(args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
