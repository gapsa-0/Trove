#!/usr/bin/env python3
"""Pre-seed a checkout with the ML model weights, and validate their manifest.

This is a developer convenience and a CI schema check, **not** a release step.
Releases used to bundle the two self-exported weights in
``packaging/models/manifest.json``, because unlike the OpenCV Zoo YOLOX detector
and the InsightFace buffalo_l pack they have no upstream download URL. That cost
349 MB of installer. They are now re-published as release assets on this
repository instead, so the app fetches them on first use like every other weight,
and ``packaging/trove.spec`` bundles nothing. Seven more entries joined them
later -- the PP-OCR weights and the Bergamot translator, which arrived inside
packages rather than inside the spec, and were removed for the same reason
(ADR 0019).

What remains useful here: staging into ``packaging/models/staged/`` populates the
second tier of ``trove.model_manifest``'s resolver, which lets a source
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
``trove.model_manifest``, which the *application* uses to resolve these
same files at runtime. They are imported rather than reimplemented here: two
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

from trove import model_manifest  # noqa: E402  (needs ROOT on sys.path)

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
        from trove.config import Config

        return Path(Config().cache_dir) / "models"
    except Exception:
        return None


def packaged_copy(item: dict) -> Path | None:
    """A copy that came in with an installed wheel, if this weight has one.

    Only the PP-OCR three do. They are the one case where the manifest mirrors
    a file that also travels inside a package: ``rapidocr`` ships its models,
    the desktop build filters them back out (ADR 0019), and the bytes are
    identical -- verified by the same SHA-256 check every other source goes
    through, so a future rapidocr that changed them fails loudly here rather
    than staging something the manifest does not describe.

    This is what lets CI exercise the OCR tests without a download, and before
    the release assets those entries point at even exist.
    """
    if not item["file"].startswith("rapidocr/"):
        return None
    try:
        import rapidocr
    except Exception:
        return None
    return Path(rapidocr.__file__).parent / "models" / Path(item["file"]).name


def candidate_sources(item: dict, override: str | None) -> list[Path]:
    roots = [Path(override)] if override else []
    cache = cache_models_dir()
    if cache is not None:
        roots.append(cache)
    candidates = [root / item["file"] for root in roots]
    packaged = packaged_copy(item)
    # Last of the local sources: an explicit --from and the user's own cache
    # both say more about intent than "whatever pip happened to install".
    if packaged is not None:
        candidates.append(packaged)
    return candidates


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


def stage(only: set[str] | None = None) -> int:
    try:
        items = models()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(error, file=sys.stderr)
        return 1
    if only is not None:
        unknown = only - {item["name"] for item in items}
        if unknown:
            print(f"no such model in the manifest: {', '.join(sorted(unknown))}", file=sys.stderr)
            return 1
        items = [item for item in items if item["name"] in only]
    STAGE.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".models-", dir=STAGE.parent) as tmp_name:
        tmp = Path(tmp_name)
        staged_info = []
        # A subset run adds to what is already there rather than replacing it.
        # The swap below is a whole-directory rename -- atomic, and the reason
        # an interrupted run never leaves a half-staged tree -- so anything not
        # carried across here would be deleted by staging one model.
        if only is not None and STAGE.is_dir():
            for existing in STAGE.rglob("*"):
                if existing.is_file():
                    carried = tmp / existing.relative_to(STAGE)
                    carried.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(existing, carried)
            info = STAGE / "models-build-info.json"
            if info.is_file():
                staged_info = [
                    entry
                    for entry in json.loads(info.read_text(encoding="utf-8")).get("models", [])
                    if entry.get("name") not in only
                ]
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
    parser.add_argument(
        "--only",
        help=(
            "comma-separated model names to stage, added to whatever is already "
            "staged. CI uses this for the PP-OCR three, which resolve out of the "
            "installed rapidocr wheel and so cost no download"
        ),
    )
    args = parser.parse_args()
    if args.validate:
        return validate()
    if args.source:
        os.environ["ARCHIVE_MODEL_SOURCE"] = args.source
    only = {name.strip() for name in args.only.split(",") if name.strip()} if args.only else None
    return stage(only)


if __name__ == "__main__":
    raise SystemExit(main())
