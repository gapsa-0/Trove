"""`oa <command>` starts with argparse: if the parser is wrong nothing downstream runs.

Nothing tested this before. That gap is not hypothetical: `oa scan` was dead on
every invocation (an `UnboundLocalError` on its very first line, from a redundant
`from pathlib import Path` inside `cmd_scan` shadowing the module-level import) and
the whole suite stayed green, because no test drove any CLI entry point -- not even
the parser that comes before dispatch. `tests/unit/test_cli_guards.py` now covers
that dispatch path; this file covers the layer beneath it, `build_parser()` itself.
That layer is the one worth pinning hardest, because the CLI is a package of one
module per command: every subparser's flags, defaults and `func` wiring is assembled
from a different file, and a command can be moved, renamed or re-registered without
any other test noticing. These tests are the contract such a change has to preserve.

Parser-only: no database, no filesystem, no `args.func(...)` calls. Milliseconds.
"""

from __future__ import annotations

import argparse

import pytest

from organize_archive.cli import build_parser, main

# The full subcommand list, asserted explicitly so a silently removed subcommand
# fails a test even though a reflective loop over "whatever the parser has" would
# not notice it went missing.
EXPECTED_SUBCOMMANDS = {
    "init",
    "scan",
    "enrich",
    "dedup",
    "faces",
    "pets",
    "dates",
    "gui",
    "status",
    "logs",
    "config",
    "migrate-data",
}


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    """Return {subcommand name: its sub-parser}, read off the built parser.

    Reflective on purpose: it walks the same `_SubParsersAction` argparse itself
    dispatches through, so it cannot describe a parser other than the real one.
    """
    [sub_action] = [
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    ]
    return dict(sub_action.choices)


# -- the subcommand set itself -----------------------------------------------


def test_defines_exactly_the_expected_subcommands():
    parser = build_parser()

    names = set(_subparsers(parser).keys())

    assert names == EXPECTED_SUBCOMMANDS


@pytest.mark.parametrize("name", sorted(EXPECTED_SUBCOMMANDS))
def test_subcommand_parses_with_no_flags(name):
    parser = build_parser()

    args = parser.parse_args([name])

    assert args.command == name


@pytest.mark.parametrize("name", sorted(EXPECTED_SUBCOMMANDS))
def test_subcommand_namespace_carries_its_dispatch_target(name):
    # dispatch in main() is `args.func(args, cfg)` -- every subparser must set it,
    # or a valid-looking parse blows up with AttributeError at dispatch time.
    parser = build_parser()

    args = parser.parse_args([name])

    assert callable(args.func)


# -- top-level options --------------------------------------------------------


def test_unknown_subcommand_exits_with_argparse_usage_error(capsys):
    parser = build_parser()

    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["not-a-real-command"])

    # argparse's own convention: 2 for a usage error, distinct from a command's
    # own return codes (which are 0/1, see test_cli_guards.py).
    assert excinfo.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_version_flag_exits_zero_and_prints_version(capsys):
    parser = build_parser()

    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["--version"])

    assert excinfo.value.code == 0
    assert "organize_archive" in capsys.readouterr().out


def test_db_override_defaults_to_none():
    parser = build_parser()

    args = parser.parse_args(["status"])

    assert args.db is None


def test_db_override_captures_the_given_path():
    parser = build_parser()

    args = parser.parse_args(["--db", "/tmp/somewhere.db", "status"])

    assert args.db == "/tmp/somewhere.db"


def test_bare_invocation_is_a_usage_error_not_a_help_screen():
    """`oa` with no arguments does NOT print help and exit 0.

    It is tempting to assume that -- it's the common argparse convention -- but
    `add_subparsers(dest="command", required=True)` makes the subcommand a
    required positional, so an empty argv is a missing-argument usage error
    (exit 2), same family as an unknown subcommand. Verified by hand before
    writing this: `main([])` raises SystemExit(2), not SystemExit(0).
    """
    with pytest.raises(SystemExit) as excinfo:
        main([])

    assert excinfo.value.code == 2


def test_help_flag_exits_zero():
    parser = build_parser()

    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["-h"])

    assert excinfo.value.code == 0


# -- per-subcommand flags ------------------------------------------------------
# One row per flag: subcommand, flag as it appears on the command line (or None
# for "no flags to pass"), the resulting args attribute, and its value. A row
# with an empty argv list pins the default; a row that passes the flag pins what
# passing it does. Table-driven so a new flag added to a subparser is an
# obviously-missing row rather than a silent gap.
FLAG_CASES = [
    # -- scan --
    ("scan", [], "root", None),
    ("scan", ["--root", "/a"], "root", ["/a"]),
    ("scan", ["--root", "/a", "--root", "/b"], "root", ["/a", "/b"]),
    ("scan", [], "no_progress", False),
    ("scan", ["--no-progress"], "no_progress", True),
    # -- enrich --
    ("enrich", [], "no_progress", False),
    ("enrich", ["--no-progress"], "no_progress", True),
    # -- dedup --
    ("dedup", [], "no_progress", False),
    ("dedup", ["--no-progress"], "no_progress", True),
    # -- faces --
    ("faces", [], "limit", None),
    ("faces", ["--limit", "50"], "limit", 50),
    ("faces", [], "recluster", False),
    ("faces", ["--recluster"], "recluster", True),
    ("faces", [], "quality_report", False),
    ("faces", ["--quality-report"], "quality_report", True),
    ("faces", [], "calibrate", None),
    # --calibrate takes an optional int: bare flag falls back to its const (100).
    ("faces", ["--calibrate"], "calibrate", 100),
    ("faces", ["--calibrate", "25"], "calibrate", 25),
    ("faces", [], "recalibrate_fiqa", False),
    ("faces", ["--recalibrate-fiqa"], "recalibrate_fiqa", True),
    ("faces", [], "migrate_adaface", False),
    ("faces", ["--migrate-adaface"], "migrate_adaface", True),
    ("faces", [], "no_progress", False),
    ("faces", ["--no-progress"], "no_progress", True),
    # -- pets --
    ("pets", [], "limit", None),
    ("pets", ["--limit", "10"], "limit", 10),
    ("pets", [], "recluster", False),
    ("pets", ["--recluster"], "recluster", True),
    ("pets", [], "no_progress", False),
    ("pets", ["--no-progress"], "no_progress", True),
    # -- gui --
    ("gui", [], "port", 8756),
    ("gui", ["--port", "9000"], "port", 9000),
    ("gui", [], "tab", False),
    ("gui", ["--tab"], "tab", True),
    ("gui", [], "no_open", False),
    ("gui", ["--no-open"], "no_open", True),
    # -- logs --
    ("logs", [], "path", False),
    ("logs", ["--path"], "path", True),
    ("logs", [], "tail", 200),
    ("logs", ["--tail", "5"], "tail", 5),
    # -- config --
    ("config", [], "show", False),
    ("config", ["--show"], "show", True),
    ("config", [], "add_root", None),
    ("config", ["--add-root", "/mnt/photos"], "add_root", "/mnt/photos"),
    ("config", [], "set_timezone", None),
    (
        "config",
        ["--set-timezone", "America/Argentina/Buenos_Aires"],
        "set_timezone",
        "America/Argentina/Buenos_Aires",
    ),
    # -- migrate-data -- (the flag is spelled --from; the code maps it to from_path
    # because `from` alone is a Python keyword and can't be an attribute name)
    ("migrate-data", [], "from_path", None),
    ("migrate-data", ["--from", "/old/data"], "from_path", "/old/data"),
]


@pytest.mark.parametrize("command, flag_args, attr, expected", FLAG_CASES)
def test_flag_produces_expected_attribute_value(command, flag_args, attr, expected):
    parser = build_parser()

    args = parser.parse_args([command, *flag_args])

    assert getattr(args, attr) == expected


# -- subcommands with no flags at all -----------------------------------------
# init, dates and status take no subcommand-specific options; pin that parsing
# them bare produces nothing surprising beyond `command` and the inherited `func`.
NO_FLAG_SUBCOMMANDS = ["init", "dates", "status"]


@pytest.mark.parametrize("name", NO_FLAG_SUBCOMMANDS)
def test_flagless_subcommand_namespace_has_only_command_db_and_func(name):
    parser = build_parser()

    args = parser.parse_args([name])

    assert vars(args).keys() == {"command", "db", "func"}
