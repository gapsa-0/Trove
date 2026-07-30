"""The `oa scan` command: walk the roots and index/hash media files."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ..config import Config
from ..db import database as db
from ..scan import walker
from ..scan.progress import ScanProgress
from ._common import _fmt_bytes


def add_parser(sub) -> None:
    sp = sub.add_parser("scan", help="Walk roots and index/hash media files (resumable)")
    sp.add_argument("--root", action="append", help="Scan this root only (repeatable)")
    sp.add_argument(
        "--no-progress", action="store_true", help="Disable the progress bar and pre-count"
    )
    sp.set_defaults(func=run)


def run(args, cfg: Config) -> int:
    if not Path(cfg.db_path).exists():
        print("No database yet. Run:  oa init")
        return 1
    if not (args.root or cfg.roots):
        print("No archive folders configured. Add one with: oa config --add-root PATH")
        return 1
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
        print("Counting files…", flush=True)
        total = sum(walker.count_files(Path(r)) for r in roots if Path(r).is_dir())
        print(f"  {total} media files to check.")
        progress = ScanProgress(total)

    totals = walker.ScanStats()
    interrupted = False
    try:
        for root in roots:
            print(f"Scanning: {root}")
            try:
                stats = walker.scan_root(
                    conn,
                    cfg,
                    root,
                    run_started,
                    progress=progress,
                    base_done=totals.seen,
                    base_bytes=totals.bytes_hashed,
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
    except KeyboardInterrupt:
        interrupted = True

    if progress is not None:
        progress.close()

    conn.execute(
        """UPDATE scan_runs SET finished_at=?, files_seen=?, files_new=?,
           files_updated=?, bytes_hashed=? WHERE id=?""",
        (db.now_iso(), totals.seen, totals.new, totals.updated, totals.bytes_hashed, run_id),
    )
    conn.commit()
    conn.close()

    if interrupted:
        print(
            "\n\nInterrupted; progress saved. Re-run 'oa scan' to resume "
            "(already-hashed files are skipped)."
        )
    print("\nScan complete:" if not interrupted else "\nProgress so far:")
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
