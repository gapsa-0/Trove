#!/usr/bin/env python3
"""Stage the ML model weights a packaged build has to carry.

Most model weights are fetched once at first run from a stable upstream URL
(OpenCV Zoo YOLOX, InsightFace buffalo_l) and need no packaging support. The
models listed in ``packaging/models/manifest.json`` are the exceptions: they have
no upstream download, so a frozen build must ship them or the feature is simply
missing for installed users.

Sources, in the order tried:

* ``--from DIR`` / ``ARCHIVE_MODEL_SOURCE`` — a directory laid out like the app's
  ``cache/models`` (a developer machine that already has the file).
* the running user's own cache directory — the usual local-build case.
* the manifest ``url``, when a maintainer has published the file as a release
  asset — the usual CI case.

Every source is SHA-256 verified against the manifest before it is staged, so all
three produce byte-identical output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "packaging" / "models" / "manifest.json"
STAGE = ROOT / "packaging" / "models" / "staged"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def models() -> list[dict]:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("models"), list):
        raise ValueError("invalid packaging/models/manifest.json schema")
    for item in data["models"]:
        if not isinstance(item, dict):
            raise ValueError("invalid model entry")
        for key in ("name", "file", "sha256", "source", "license"):
            if not isinstance(item.get(key), str) or not item[key]:
                raise ValueError(f"model entry missing {key}")
        relative = Path(item["file"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe model path: {item['file']}")
        if len(item["sha256"]) != 64 or any(
            c not in "0123456789abcdefABCDEF" for c in item["sha256"]
        ):
            raise ValueError(f"invalid SHA-256 for model {item['name']}")
        if not isinstance(item.get("size"), int) or item["size"] <= 0:
            raise ValueError(f"invalid size for model {item['name']}")
        url = item.get("url")
        if url is not None and (not isinstance(url, str) or not url.startswith("https://")):
            raise ValueError(f"invalid url for model {item['name']}")
    return data["models"]


def validate() -> int:
    try:
        models()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(error, file=sys.stderr)
        return 1
    print("model manifest schema OK")
    return 0


def cache_models_dir() -> Path | None:
    """The app's own ``cache/models`` directory, if this checkout can tell us."""
    try:
        sys.path.insert(0, str(ROOT))
        from organize_archive.config import Config  # noqa: PLC0415

        return Path(Config().cache_dir) / "models"
    except Exception:
        return None


def candidate_sources(item: dict, override: str | None) -> list[Path]:
    roots = [Path(override)] if override else []
    cache = cache_models_dir()
    if cache is not None:
        roots.append(cache)
    return [root / item["file"] for root in roots]


def fetch(item: dict, destination: Path) -> str:
    """Put a hash-verified copy of ``item`` at ``destination``; say where from."""
    for candidate in candidate_sources(item, os.environ.get("ARCHIVE_MODEL_SOURCE")):
        if candidate.is_file():
            actual = sha256(candidate)
            if actual != item["sha256"].lower():
                raise ValueError(
                    f"{item['name']}: {candidate} does not match the manifest "
                    f"(got {actual}). Re-export it or fix the manifest."
                )
            shutil.copy2(candidate, destination)
            return str(candidate)
    url = item.get("url")
    if url:
        urllib.request.urlretrieve(url, destination)
        actual = sha256(destination)
        if actual != item["sha256"].lower():
            raise ValueError(f"{item['name']}: SHA-256 mismatch from {url} (got {actual})")
        return url
    raise ValueError(
        f"{item['name']} is not available.\n"
        f"  It has no download URL in packaging/models/manifest.json and no local\n"
        f"  copy was found ({item['file']}).\n"
        f"  Fix by either: publishing the file as a release asset and recording its\n"
        f"  https URL in the manifest (needed for CI builds), or building from a\n"
        f"  machine that has it and passing --from <cache/models dir>.\n"
        f"  Regenerate it with: python3 {item['source'].split(' —')[0]}"
    )


def stage() -> int:
    try:
        items = models()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(error, file=sys.stderr)
        return 1
    STAGE.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".models-", dir=STAGE.parent) as tmp_name:
        tmp = Path(tmp_name)
        staged_info = []
        for item in items:
            destination = tmp / item["file"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                origin = fetch(item, destination)
            except (OSError, ValueError, urllib.error.URLError) as error:
                print(error, file=sys.stderr)
                return 1
            staged_info.append(
                {
                    "name": item["name"],
                    "file": item["file"],
                    "sha256": item["sha256"].lower(),
                    "license": item["license"],
                    "source": item["source"],
                    "staged_from": origin,
                }
            )
            print(f"staged {item['name']} from {origin}")
        (tmp / "models-build-info.json").write_text(
            json.dumps({"models": staged_info}, indent=2) + "\n", encoding="utf-8"
        )
        if STAGE.exists():
            shutil.rmtree(STAGE)
        os.replace(tmp, STAGE)
    print(STAGE)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="source", help="directory laid out like cache/models")
    parser.add_argument("--validate", action="store_true", help="check the manifest schema only")
    args = parser.parse_args()
    if args.validate:
        return validate()
    if args.source:
        os.environ["ARCHIVE_MODEL_SOURCE"] = args.source
    return stage()


if __name__ == "__main__":
    raise SystemExit(main())
