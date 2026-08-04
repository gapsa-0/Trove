"""The `oa logs` command: where the log is, or what it last said."""

from __future__ import annotations

import argparse

from .. import logging_setup
from ..config import Config


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    sp = sub.add_parser("logs", help="Print the last lines of the log, or where it lives")
    sp.add_argument("--path", action="store_true", help="Print the log file's path and exit")
    sp.add_argument(
        "--tail",
        type=int,
        default=200,
        metavar="N",
        help="Print the last N lines (default 200; 0 for the whole file)",
    )
    sp.set_defaults(func=run)


def run(args: argparse.Namespace, cfg: Config) -> int:
    """Where the log is, or what it last said.

    Exists to turn a support exchange from "navigate to your application data
    folder, which is somewhere different on each OS" into one command. No
    database is needed or touched.
    """
    path = logging_setup.log_file()
    if args.path:
        print(path)
        return 0
    if not path.is_file():
        print(f"No log file yet at {path}")
        print("It is created the first time something is logged.")
        return 1
    # Whole-file read: rotation caps this at 5 MB (logging_setup.MAX_BYTES), so
    # there is no point in a seek-backwards tail. Rotated files (trove.log.1 and
    # friends) sit next to it and are deliberately not merged in -- interleaving
    # them correctly needs parsing, and this command is for "what just happened".
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-args.tail :] if args.tail > 0 else lines:
        print(line)
    return 0
