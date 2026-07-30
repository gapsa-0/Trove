"""Every command that needs a database must refuse politely when there is none.

This exists because of a real bug: `cmd_scan` opened with
`if not Path(cfg.db_path).exists()` while also carrying a redundant
`from pathlib import Path` further down its own body. That local import makes
`Path` a function-local name for the *whole* function, so the guard on the first
line raised `UnboundLocalError` instead of printing the hint -- `oa scan` was
dead on every invocation and no test noticed.

The check is therefore deliberately shallow and broad: dispatch each guarded
command through the real parser with a database path that does not exist, and
assert it returns 1 with the hint. That catches the shadowing class of bug in
any of them, not just the one that was broken.
"""

from __future__ import annotations

import pytest

from organize_archive import cli
from organize_archive.config import Config

GUARDED = ["scan", "enrich", "dedup", "faces", "pets", "dates", "status"]


@pytest.mark.parametrize("command", GUARDED)
def test_guarded_command_reports_missing_database(command, tmp_path, capsys):
    cfg = Config()
    cfg.db_path = str(tmp_path / "definitely-not-created.db")

    args = cli.build_parser().parse_args([command])
    rc = args.func(args, cfg)

    assert rc == 1
    assert "No database yet" in capsys.readouterr().out
