"""Entry point for ``python -m organize_archive``: delegates straight to the CLI."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
