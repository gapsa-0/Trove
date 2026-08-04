"""Entry point for ``python -m trove``: delegates straight to the CLI."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
