"""Loopback-only backend entry point used by the Archive desktop shell."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading

from . import __version__
from .config import Config
from .gui.server import serve


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="organize-archive-backend")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.host != "127.0.0.1":
        print("desktop backend may bind only to 127.0.0.1", file=sys.stderr)
        return 2
    if not 0 <= args.port <= 65535:
        print("port must be between 0 and 65535", file=sys.stderr)
        return 2

    httpd = serve(Config.load(), host=args.host, port=args.port)
    stopping = threading.Event()

    def stop(_signum, _frame):
        if stopping.is_set():
            return
        stopping.set()
        # shutdown() must run outside serve_forever's serving thread.
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    actual_port = httpd.server_address[1]
    build = {"version": __version__, "commit": os.environ.get("ARCHIVE_BUILD_COMMIT", "dev")}
    print(f"READY {json.dumps({'port': actual_port, **build})}", flush=True)
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.RequestHandlerClass.jobs.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
