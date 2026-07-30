"""The `oa migrate-data` command: move a pre-XDG data directory into place."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from ..config import PROJECT_ROOT, Config
from ..paths import app_data_dir


def add_parser(sub) -> None:
    sp = sub.add_parser(
        "migrate-data", help="Copy legacy project-local data into user application data"
    )
    sp.add_argument(
        "--from",
        dest="from_path",
        metavar="PATH",
        help="Legacy data directory (defaults to this project's data/)",
    )
    sp.set_defaults(func=run)


def run(args, cfg: Config) -> int:
    source = Path(args.from_path).expanduser() if args.from_path else _legacy_data_dir()
    if not source.exists() or not source.is_dir():
        if args.from_path:
            print(
                f"Migration source does not exist or is not a directory: {source}", file=sys.stderr
            )
        else:
            print(
                "No legacy project-local data directory was found. "
                "Specify one with: oa migrate-data --from PATH",
                file=sys.stderr,
            )
        return 1

    artefacts = (source / "config.json", source / "archive.db", source / "cache")
    if not any(path.is_file() if path.name != "cache" else path.is_dir() for path in artefacts):
        print("Migration source contains none of: config.json, archive.db, cache/", file=sys.stderr)
        return 1

    target = app_data_dir()
    target_artefacts = (target / "config.json", target / "archive.db", target / "cache")
    if any(path.exists() for path in target_artefacts):
        print(f"Migration target already contains application data: {target}", file=sys.stderr)
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


def _legacy_data_dir() -> Path:
    """Return the old repository-local data directory used before Step 1."""
    return PROJECT_ROOT / "data"
