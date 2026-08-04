"""Loopback-only backend entry point used by the Trove desktop shell."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import threading
from types import FrameType
from typing import cast

from . import __version__, logging_setup
from .app_data_migration import migrate_legacy_app_data
from .config import Config
from .web.server import Handler, serve

# Named explicitly, not __name__: this module is the console-script entry point
# (trove-backend) in a packaged build but `python -m
# trove.desktop` in a source run, where __name__ is "__main__". The
# logger name is meant to say where a line came from, so it must not depend on
# how the process was started.
logger = logging.getLogger("trove.desktop")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trove-backend")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    return parser


def main(argv: list[str] | None = None) -> int:
    # The other of the two places allowed to configure logging. The desktop
    # shell captures this process's stderr into backend-stderr.log and shows it
    # behind "Copy diagnostics", so everything logged here reaches a user's bug
    # report without them having to find a file.
    logging_setup.configure()
    args = build_parser().parse_args(argv)
    if args.host != "127.0.0.1":
        logger.error("desktop backend may bind only to 127.0.0.1")
        return 2
    if not 0 <= args.port <= 65535:
        logger.error("port must be between 0 and 65535")
        return 2

    # Before load(), for the same reason as in cli.main: an install predating
    # the rename keeps its config under the old directory name.
    migrate_legacy_app_data()
    httpd = serve(Config.load(), host=args.host, port=args.port)
    stopping = threading.Event()

    def stop(_signum: int, _frame: FrameType | None) -> None:
        if stopping.is_set():
            return
        stopping.set()
        # shutdown() must run outside serve_forever's serving thread.
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    actual_port = httpd.server_address[1]
    build = {"version": __version__, "commit": os.environ.get("ARCHIVE_BUILD_COMMIT", "dev")}
    logger.info(
        "backend listening version=%s commit=%s host=%s port=%s",
        build["version"],
        build["commit"],
        args.host,
        actual_port,
    )
    # NOT a log call: this line on *stdout* is the readiness handshake the
    # desktop shell blocks on (see validReady() in desktop/src/main.cjs, which
    # times out after 20s). Routing it through logging would send it to stderr
    # and hang every app launch.
    print(f"READY {json.dumps({'port': actual_port, **build})}", flush=True)  # noqa: T201
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        # serve() binds the JobManager onto the handler subclass it builds, and
        # this is the only handle on it out here. RequestHandlerClass is typed
        # as a plain callable, so the cast is what names what it really is.
        cast(type[Handler], httpd.RequestHandlerClass).jobs.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
