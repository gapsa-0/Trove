"""The `oa config` command: show or modify configuration."""

from __future__ import annotations

import json

from ..config import Config


def add_parser(sub) -> None:
    sp = sub.add_parser("config", help="Show or modify configuration")
    sp.add_argument("--show", action="store_true", help="Print current config")
    sp.add_argument("--add-root", metavar="PATH", help="Add a source root")
    sp.add_argument(
        "--set-timezone",
        metavar="IANA",
        help="Set timezone for Takeout date conversion (e.g. America/Argentina/Buenos_Aires)",
    )
    sp.set_defaults(func=run)


def run(args, cfg: Config) -> int:
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
            # Broad for the same reason as metadata/resolver.py's _tz: ZoneInfo
            # can raise ZoneInfoNotFoundError, ValueError, or OSError from the
            # tzdata lookup, and narrowing without proof of the full set would
            # risk turning a bad-but-reportable input into an uncaught crash.
            # The user is told directly below, so no log call is needed here.
            print(
                f"Unknown timezone: {args.set_timezone!r} "
                "(use an IANA name like America/Argentina/Buenos_Aires)"
            )
            return 1
        cfg.timezone = args.set_timezone
        cfg.save()
        print(
            f"Timezone set to {args.set_timezone}. Re-run 'oa enrich' to apply it to Takeout dates."
        )
    return 0
