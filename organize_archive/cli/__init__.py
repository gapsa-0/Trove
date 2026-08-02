"""Command-line interface for organize_archive.

Runs on the standard library alone. `rich` is used for prettier output when
installed, but the tool degrades gracefully without it.
"""

from __future__ import annotations

import argparse
import logging
import sys

from .. import __version__, logging_setup
from ..config import Config
from ..db import database as db
from ..paths import config_file
from ..runtime import tool as runtime_tool
from . import (
    config,
    dates,
    dedup,
    enrich,
    faces,
    gui,
    logs,
    migrate,
    pets,
    scan,
    status,
)

# Named explicitly, not __name__: `oa` reaches main() as an imported module but
# `python -m organize_archive.cli` makes it "__main__", and a log line should
# not change its origin depending on how the process was started.
logger = logging.getLogger("organize_archive.cli")


# -- commands ---------------------------------------------------------------


def _preflight() -> list[str]:
    """Return a list of missing-but-recommended system tools."""
    missing = []
    for name in ("exiftool", "ffprobe"):
        if runtime_tool(name) is None:
            missing.append(name)
    return missing


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
        print("No archive folders configured. Add one with: oa config --add-root PATH")
    missing = _preflight()
    if missing:
        print(
            f"\nNote: optional tools not found: {', '.join(missing)} "
            f"(needed for metadata/video in later phases)."
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="oa", description=__doc__)
    p.add_argument("--version", action="version", version=f"organize_archive {__version__}")
    p.add_argument(
        "--db",
        metavar="PATH",
        help="Use this database file instead of the configured default "
        "(useful for isolated testing while a scan runs).",
    )
    # Not `required=True`: that turns a bare `oa` into "error: the following
    # arguments are required: command" on stderr with exit 2, which is the
    # least helpful thing to show someone who typed the name of the tool to
    # find out what it does. main() prints the help screen instead. An
    # *unknown* subcommand is still a usage error -- that one is a mistake,
    # not a question.
    sub = p.add_subparsers(dest="command")

    sp = sub.add_parser("init", help="Create the database and register roots")
    sp.set_defaults(func=cmd_init)

    scan.add_parser(sub)

    enrich.add_parser(sub)

    dedup.add_parser(sub)

    faces.add_parser(sub)

    pets.add_parser(sub)

    dates.add_parser(sub)

    gui.add_parser(sub)

    status.add_parser(sub)

    logs.add_parser(sub)

    config.add_parser(sub)

    migrate.add_parser(sub)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    # One of the only two places in the codebase that may configure logging;
    # every library module just does getLogger(__name__). Before Config.load(),
    # so anything that load() has to report about a broken or migrating config
    # is already being recorded.
    logging_setup.configure()
    logger.debug("oa %s", " ".join(argv if argv is not None else sys.argv[1:]))
    cfg = Config.load()
    if args.db:
        cfg.db_path = args.db
    return args.func(args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
