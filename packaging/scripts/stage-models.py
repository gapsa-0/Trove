#!/usr/bin/env python3
"""Pre-seed a checkout with the ML model weights, and validate their manifest.

This is a developer convenience and a CI schema check, **not** a release step.
Releases used to bundle the two weights in ``packaging/models/manifest.json``,
because unlike the OpenCV Zoo YOLOX detector and the InsightFace buffalo_l pack
they have no upstream download URL. That cost 349 MB of installer. They are now
re-published as release assets on this repository instead, so the app fetches them
on first use like every other weight, and ``packaging/organize-archive.spec``
bundles nothing.

What remains useful here: staging into ``packaging/models/staged/`` populates the
second tier of ``organize_archive.model_manifest``'s resolver, which lets a source
checkout run the model-backed tests offline; and ``--validate`` is what CI runs to
keep the manifest honest. Nothing in the desktop build calls this script.

Sources, in the order tried:

* ``--from DIR`` / ``ARCHIVE_MODEL_SOURCE`` — a directory laid out like the app's
  ``cache/models`` (a developer machine that already has the file).
* the running user's own cache directory — the usual local-build case.
* the manifest ``url``, when a maintainer has published the file as a release
  asset — the usual CI case.

Every source is SHA-256 verified against the manifest before it is staged, so all
three produce byte-identical output.

The manifest schema, the hashing and the URL download live in
``organize_archive.model_manifest``, which the *application* uses to resolve these
same two files at runtime. They are imported rather than reimplemented here: two
copies of "what a valid entry is" would eventually disagree, and the one that
matters is whichever the app believes.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from organize_archive import model_manifest  # noqa: E402  (needs ROOT on sys.path)

MANIFEST = model_manifest.MANIFEST_PATH
STAGE = model_manifest.STAGED_DIR
sha256 = model_manifest.sha256


def models() -> list[dict]:
    return model_manifest.load(MANIFEST)


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
        from organize_archive.config import Config

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
