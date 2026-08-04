"""The `trove dates` command: files-per-year and date-source summary."""

from __future__ import annotations

import argparse
from pathlib import Path

from ..config import Config
from ..db import database as db


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    sp = sub.add_parser("dates", help="Show files-per-year and date-source summary")
    sp.set_defaults(func=run)


def run(args: argparse.Namespace, cfg: Config) -> int:
    if not Path(cfg.db_path).exists():
        print("No database yet. Run:  trove init  then  trove scan")
        return 1
    conn = db.open_readonly(cfg.db_path)

    done = conn.execute("SELECT COUNT(*) FROM dates").fetchone()[0]
    if not done:
        print("No dates resolved yet. Run:  trove enrich")
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
