"""Command-line interface for organize_archive.

Runs on the standard library alone. `rich` is used for prettier output when
installed, but the tool degrades gracefully without it.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from . import __version__
from .config import Config, PROJECT_ROOT
from .paths import app_data_dir, config_file
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
    if not config_file().exists():
        # ``--db`` is deliberately transient (useful for isolated runs), so do
        # not turn that command-line override into the saved default.
        (Config() if args.db else cfg).save()
    print(f"Initialized database at {cfg.db_path}")
    print(f"Cache directory:        {cfg.cache_dir}")
    if cfg.roots:
        print(f"Configured roots:       {', '.join(cfg.roots)}")
    else:
        print("No archive folders configured. Add one with: "
              "oa config --add-root PATH")
    missing = _preflight()
    if missing:
        print(f"\nNote: optional tools not found: {', '.join(missing)} "
              f"(needed for metadata/video in later phases).")
    return 0


def cmd_scan(args, cfg: Config) -> int:
    if not Path(cfg.db_path).exists():
        print("No database yet. Run:  oa init")
        return 1
    if not (args.root or cfg.roots):
        print("No archive folders configured. Add one with: "
              "oa config --add-root PATH")
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
        from pathlib import Path
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
    except KeyboardInterrupt:
        interrupted = True

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

    if interrupted:
        print("\n\nInterrupted — progress saved. Re-run 'oa scan' to resume "
              "(already-hashed files are skipped).")
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


def cmd_enrich(args, cfg: Config) -> int:
    from pathlib import Path
    from .metadata import enrich as enrich_mod
    from .metadata.exiftool_reader import available as exif_available

    if not Path(cfg.db_path).exists():
        print("No database yet. Run:  oa init  then  oa scan")
        return 1

    if not exif_available():
        print("Note: exiftool not found — resolving dates from Takeout JSON, "
              "filenames and file times only (no EXIF).")
    if cfg.timezone is None:
        print("Note: no timezone set — Takeout (UTC) dates may shift evening "
              "photos by a day. Set one with:  oa config --set-timezone <IANA>")

    conn = db.connect(cfg.db_path)
    db.init_db(conn)

    progress = None if args.no_progress else ScanProgress(
        None, show_bytes=False, label="enriching")
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


def cmd_dedup(args, cfg: Config) -> int:
    from pathlib import Path
    from .dedup import exact
    from .scan.progress import ScanProgress
    if not Path(cfg.db_path).exists():
        print("No database yet. Run:  oa init  then  oa scan")
        return 1
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    progress = None if args.no_progress else ScanProgress(
        None, show_bytes=False, label="grouping")
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
        print("  note: visual matching needs the optional media dependencies "
              "(install with: pip install '.[media]')")
    return 0


def cmd_faces(args, cfg: Config) -> int:
    from pathlib import Path
    from .faces import backend, extract as fx, cluster as fc
    if not Path(cfg.db_path).exists():
        print("No database yet. Run:  oa init  then  oa scan  then  oa enrich")
        return 1
    if not backend.available():
        print("Face detection needs OpenCV's DNN face APIs. Install a modern "
              "opencv-python (the 'media' extra) and retry.")
        return 1

    conn = db.connect(cfg.db_path)
    db.init_db(conn)

    if not args.recluster:
        if not backend.models_ready(cfg.cache_dir):
            print("Fetching face models (one-time, ~38 MB) into "
                  f"{cfg.cache_dir}/models …")
        pending = fx.pending_count(conn)
        if pending == 0:
            print("All images already face-scanned.")
        else:
            cap = f" (limit {args.limit})" if args.limit else ""
            print(f"Detecting faces in {pending} image(s){cap} …")
            progress = None if args.no_progress else ScanProgress(
                None, show_bytes=False, label="faces")
            es = fx.extract(conn, cfg, progress=progress, limit=args.limit)
            if progress is not None:
                progress.close()
            print(f"\n  images scanned    : {es.processed}")
            print(f"  faces detected    : {es.faces_found}")
            print(f"  photos with faces : {es.images_with_faces}")
            if es.errors:
                print(f"  errors            : {es.errors}")
                for s in es.error_samples:
                    print(f"      - {s}")

    print("\nClustering faces into people …")
    progress = None if args.no_progress else ScanProgress(
        None, show_bytes=False, label="clustering")
    cs = fc.cluster_faces(conn, cfg, progress=progress)
    if progress is not None:
        progress.close()
    conn.close()
    print(f"\n  people found      : {cs.people}")
    print(f"  faces clustered   : {cs.clustered}")
    print(f"  unassigned faces  : {cs.noise}")
    if cs.named:
        print(f"  names preserved   : {cs.named}")
    return 0


def cmd_dates(args, cfg: Config) -> int:
    from pathlib import Path
    if not Path(cfg.db_path).exists():
        print("No database yet. Run:  oa init  then  oa scan")
        return 1
    conn = db.open_readonly(cfg.db_path)

    done = conn.execute("SELECT COUNT(*) FROM dates").fetchone()[0]
    if not done:
        print("No dates resolved yet. Run:  oa enrich")
        return 0

    print("Files per year:")
    rows = conn.execute(
        """SELECT substr(best_datetime,1,4) AS y, COUNT(*) c
           FROM dates WHERE best_datetime IS NOT NULL
           GROUP BY y ORDER BY y"""
    ).fetchall()
    peak = max((r["c"] for r in rows), default=1)
    for r in rows:
        bar = "█" * max(1, round(30 * r["c"] / peak))
        print(f"  {r['y']}  {r['c']:>7}  {bar}")

    print("\nDate source:")
    for r in conn.execute(
        "SELECT date_source, COUNT(*) c FROM dates GROUP BY date_source ORDER BY c DESC"
    ):
        print(f"  {r['date_source']:<14} {r['c']:>7}")

    gps = conn.execute("SELECT COUNT(*) FROM geo").fetchone()[0]
    print(f"\nWith GPS location: {gps}")
    conn.close()
    return 0


def cmd_gui(args, cfg: Config) -> int:
    from pathlib import Path
    from .gui.server import serve
    from .gui import launcher

    if not Path(cfg.db_path).exists():
        print("No database yet. Run:  oa init  then  oa scan  then  oa enrich")
        return 1

    httpd = serve(cfg, port=args.port)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"organize_archive GUI running at {url}")
    print("Press Ctrl-C to stop.")
    if not args.no_open:
        how = launcher.open_url(url, app_mode=not args.tab)
        if how == "app-window":
            print("Opened in a standalone app window.")
        elif not args.tab:
            print("(No Chrome/Chromium/Edge found — opened a browser tab instead.)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        httpd.server_close()
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
    if args.set_timezone:
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(args.set_timezone)
        except Exception:
            print(f"Unknown timezone: {args.set_timezone!r} "
                  "(use an IANA name like America/Argentina/Buenos_Aires)")
            return 1
        cfg.timezone = args.set_timezone
        cfg.save()
        print(f"Timezone set to {args.set_timezone}. Re-run 'oa enrich' to apply "
              "it to Takeout dates.")
    return 0


def _legacy_data_dir() -> Path:
    """Return the old repository-local data directory used before Step 1."""
    return PROJECT_ROOT / "data"


def cmd_migrate_data(args, cfg: Config) -> int:
    source = Path(args.from_path).expanduser() if args.from_path else _legacy_data_dir()
    if not source.exists() or not source.is_dir():
        if args.from_path:
            print(f"Migration source does not exist or is not a directory: {source}",
                  file=sys.stderr)
        else:
            print("No legacy project-local data directory was found. "
                  "Specify one with: oa migrate-data --from PATH", file=sys.stderr)
        return 1

    artefacts = (source / "config.json", source / "archive.db", source / "cache")
    if not any(path.is_file() if path.name != "cache" else path.is_dir()
               for path in artefacts):
        print("Migration source contains none of: config.json, archive.db, cache/",
              file=sys.stderr)
        return 1

    target = app_data_dir()
    target_artefacts = (target / "config.json", target / "archive.db", target / "cache")
    if any(path.exists() for path in target_artefacts):
        print(f"Migration target already contains application data: {target}",
              file=sys.stderr)
        print("Refusing to merge existing data automatically.", file=sys.stderr)
        return 1

    target.mkdir(parents=True, exist_ok=True)
    for name in ("config.json", "archive.db"):
        item = source / name
        if item.is_file():
            shutil.copy2(item, target / name)
    cache = source / "cache"
    if cache.is_dir():
        shutil.copytree(cache, target / "cache")

    print("Migrated application data (copied; original was kept):")
    print(f"  Source:      {source}")
    print(f"  Destination: {target}")
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

    sp = sub.add_parser("enrich", help="Resolve dates, GPS and metadata (resumable)")
    sp.add_argument("--no-progress", action="store_true", help="Disable progress bar")
    sp.set_defaults(func=cmd_enrich)

    sp = sub.add_parser("dedup", help="Group exact duplicates and visually identical image variants")
    sp.add_argument("--no-progress", action="store_true", help="Disable progress bar")
    sp.set_defaults(func=cmd_dedup)

    sp = sub.add_parser("faces", help="Detect faces (local) and cluster them into people")
    sp.add_argument("--limit", type=int, default=None,
                    help="Only scan this many pending images this run (resumable)")
    sp.add_argument("--recluster", action="store_true",
                    help="Skip detection; just re-cluster existing faces into people")
    sp.add_argument("--no-progress", action="store_true", help="Disable progress bar")
    sp.set_defaults(func=cmd_faces)

    sp = sub.add_parser("dates", help="Show files-per-year and date-source summary")
    sp.set_defaults(func=cmd_dates)

    sp = sub.add_parser("gui", help="Launch the local web UI (standalone window)")
    sp.add_argument("--port", type=int, default=8756, help="Port (default 8756)")
    sp.add_argument("--tab", action="store_true",
                    help="Open a normal browser tab instead of an app window")
    sp.add_argument("--no-open", action="store_true", help="Don't open anything")
    sp.set_defaults(func=cmd_gui)

    sp = sub.add_parser("status", help="Show catalog summary")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("config", help="Show or modify configuration")
    sp.add_argument("--show", action="store_true", help="Print current config")
    sp.add_argument("--add-root", metavar="PATH", help="Add a source root")
    sp.add_argument("--set-timezone", metavar="IANA",
                    help="Set timezone for Takeout date conversion "
                         "(e.g. America/Argentina/Buenos_Aires)")
    sp.set_defaults(func=cmd_config)

    sp = sub.add_parser("migrate-data",
                        help="Copy legacy project-local data into user application data")
    sp.add_argument("--from", dest="from_path", metavar="PATH",
                    help="Legacy data directory (defaults to this project's data/)")
    sp.set_defaults(func=cmd_migrate_data)

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
