"""The `trove gui` command: serve the local web UI and open a window on it."""

from __future__ import annotations

import argparse

from ..config import Config


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    sp = sub.add_parser("gui", help="Launch the local web UI (standalone window)")
    sp.add_argument("--port", type=int, default=8756, help="Port (default 8756)")
    sp.add_argument(
        "--tab", action="store_true", help="Open a normal browser tab instead of an app window"
    )
    sp.add_argument("--no-open", action="store_true", help="Don't open anything")
    sp.set_defaults(func=run)


def run(args: argparse.Namespace, cfg: Config) -> int:
    from ..web import launcher
    from ..web.server import serve

    httpd = serve(cfg, port=args.port)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"trove GUI running at {url}")
    print("Press Ctrl-C to stop.")
    if not args.no_open:
        how = launcher.open_url(url, app_mode=not args.tab)
        if how == "app-window":
            print("Opened in a standalone app window.")
        elif not args.tab:
            print("(No Chrome/Chromium/Edge found; opened a browser tab instead.)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        httpd.server_close()
    return 0
