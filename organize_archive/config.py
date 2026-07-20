"""Configuration: source roots, data/cache locations, ignore rules, thresholds.

Defaults keep everything self-contained inside the project folder (data/), so
nothing is ever written among the originals. An optional JSON file at
``data/config.json`` overrides the defaults.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

# Project layout ------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_DB_PATH = DATA_DIR / "archive.db"
DEFAULT_CACHE_DIR = DATA_DIR / "cache"
CONFIG_FILE = DATA_DIR / "config.json"

# The archive to catalog (read-only). Multiple roots are supported.
DEFAULT_ROOTS = ["/media/capsa/Residuos/Multimedia"]

# Files that are not media content. Google Takeout ``.json`` sidecars are
# excluded here as *content* — they are consumed as metadata in Phase 3.
IGNORE_EXTENSIONS = {
    "json", "db", "thm", "ini", "nomedia", "part", "tmp",
}
IGNORE_FILENAMES = {
    "thumbs.db", "desktop.ini", ".nomedia", ".picasa.ini",
    "picasa.ini", ".ds_store",
}
# Substrings that mark Google/Picasa/Android index & housekeeping leftovers
# (these often have no extension, e.g. "thumbdata3-123", "nomedia_1620517712...").
IGNORE_NAME_SUBSTRINGS = (
    "thumbindex", "thumbdata", "database_uuid", "nomedia",
    ".com.google.chrome.",  # browser temp download leftovers
)


@dataclass
class Config:
    roots: list[str] = field(default_factory=lambda: list(DEFAULT_ROOTS))
    db_path: str = str(DEFAULT_DB_PATH)
    cache_dir: str = str(DEFAULT_CACHE_DIR)

    # Hashing
    fast_hash_sample_bytes: int = 65536  # head+tail sample for the cheap prefilter

    # Dedup (Phase 4)
    phash_hamming_threshold: int = 6

    # Date resolution priority (Phase 3). Tunable; first available wins.
    date_priority: list[str] = field(
        default_factory=lambda: ["takeout_json", "exif", "filename", "mtime"]
    )
    # IANA timezone (e.g. "America/Argentina/Buenos_Aires") used to convert
    # Google Takeout's UTC timestamps to local wall-clock time, so evening
    # photos don't roll into the next day. None => keep UTC.
    timezone: str | None = None

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text())
            for k, v in data.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
        return cfg

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(asdict(self), indent=2))

    def ensure_dirs(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.cache_dir).mkdir(parents=True, exist_ok=True)
