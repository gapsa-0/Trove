"""The `oa status` command: what the catalog currently holds."""

from __future__ import annotations

from pathlib import Path

from .. import __version__
from ..config import Config
from ..db import database as db
from ._common import _fmt_bytes


def add_parser(sub) -> None:
    sp = sub.add_parser("status", help="Show catalog summary")
    sp.set_defaults(func=run)


def run(args, cfg: Config) -> int:
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
