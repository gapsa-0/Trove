#!/usr/bin/env python3
"""Fetch, verify and stage native media tools for a release target.

Archives are downloaded to a temporary directory, SHA-256 verified before any
extraction, and staged atomically.  Downloaded archives and staged binaries are
deliberately ignored by Git; the manifest records the reproducible inputs.

FFmpeg is staged from a *shared* build: small ``ffmpeg``/``ffprobe`` binaries plus
the ``libav*`` libraries they both link against.  The static build put the whole
codec set inside each of the two binaries -- 266 MB to ship one copy of FFmpeg
twice.  Everything a tool needs is staged flat in one directory, which is the
layout both platforms load from; see ``runtime_libs`` below and
``trove.runtime.tool_env``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from trove import runtime  # noqa: E402  (needs ROOT on sys.path)

MANIFEST = ROOT / "packaging" / "tools" / "manifest.json"
REQUIRED_TOOL_NAMES = {"ffmpeg", "ffprobe"}
OPTIONAL_TOOL_NAMES = {"exiftool"}
KNOWN_TOOL_NAMES = REQUIRED_TOOL_NAMES | OPTIONAL_TOOL_NAMES


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def target_data(target: str) -> tuple[list[dict], list[dict]]:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("targets"), dict):
        raise ValueError("invalid packaging/tools/manifest.json schema")
    entry = data["targets"].get(target)
    if not entry:
        raise ValueError(f"no approved tool payload for {target}")
    # Keep accepting the original list-only form while allowing an explicit
    # unavailable tool declaration for short-lived, feature-limited betas.
    if isinstance(entry, list):
        tools, unavailable = entry, []
    elif isinstance(entry, dict):
        tools, unavailable = entry.get("tools"), entry.get("unavailable", [])
    else:
        raise ValueError(f"invalid target entry for {target}")
    if not isinstance(tools, list) or not isinstance(unavailable, list):
        raise ValueError(f"invalid target entry for {target}")
    return tools, unavailable


def validate_target(target: str) -> tuple[list[dict], list[dict]]:
    tools, unavailable = target_data(target)
    names: set[str] = set()
    for item in tools:
        if not isinstance(item, dict):
            raise ValueError(f"invalid tool entry for {target}")
        required = ("name", "version", "url", "sha256", "license", "executable")
        if not all(isinstance(item.get(key), str) and item[key] for key in required):
            raise ValueError(
                f"invalid tool entry for {target}: required fields are {', '.join(required)}"
            )
        if item["name"] not in KNOWN_TOOL_NAMES or item["name"] in names:
            raise ValueError(f"invalid or duplicate tool name for {target}: {item['name']!r}")
        if len(item["sha256"]) != 64 or any(
            c not in "0123456789abcdefABCDEF" for c in item["sha256"]
        ):
            raise ValueError(f"invalid SHA-256 for {target}: {item['name']}")
        # Optional: stage the archive member under a different name, and copy a
        # sibling runtime directory along with it (ExifTool's Windows build is
        # ``exiftool(-k).exe`` plus a required ``exiftool_files/`` tree).
        for key in ("install_as", "support_dir"):
            value = item.get(key)
            if value is None:
                continue
            if not isinstance(value, str) or not value:
                raise ValueError(f"invalid {key} for {target}: {item['name']}")
            if Path(value).is_absolute() or len(Path(value).parts) != 1 or value in (".", ".."):
                raise ValueError(f"{key} must be a plain file name for {target}: {item['name']}")
        # Optional: shared libraries the executable needs, as globs relative to
        # the *archive root* rather than to the executable. That is the difference
        # from support_dir, and it is why this field has to exist: the shared
        # FFmpeg build puts bin/ffmpeg and lib/*.so.* side by side, so the
        # libraries are not reachable from the executable's own directory.
        libs = item.get("runtime_libs")
        if libs is not None:
            if not isinstance(libs, list) or not libs:
                raise ValueError(f"invalid runtime_libs for {target}: {item['name']}")
            for pattern in libs:
                if not isinstance(pattern, str) or not pattern:
                    raise ValueError(f"invalid runtime_libs entry for {target}: {item['name']}")
                parts = Path(pattern).parts
                if Path(pattern).is_absolute() or ".." in parts:
                    raise ValueError(
                        f"runtime_libs must be relative and stay inside the archive "
                        f"for {target}: {item['name']}"
                    )
        names.add(item["name"])
    unavailable_names: set[str] = set()
    for item in unavailable:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or not isinstance(item.get("reason"), str)
        ):
            raise ValueError(f"invalid unavailable tool entry for {target}")
        if (
            item["name"] not in OPTIONAL_TOOL_NAMES
            or item["name"] in unavailable_names
            or item["name"] in names
        ):
            raise ValueError(f"invalid unavailable tool declaration for {target}: {item['name']!r}")
        unavailable_names.add(item["name"])
    missing = REQUIRED_TOOL_NAMES - names
    if missing:
        raise ValueError(
            f"{target} is missing required tool payloads: {', '.join(sorted(missing))}"
        )
    return tools, unavailable


def validate() -> int:
    try:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        if data.get("schema_version") != 1 or not isinstance(data.get("targets"), dict):
            raise ValueError("invalid packaging/tools/manifest.json schema")
        for target in data["targets"]:
            validate_target(target)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(error, file=sys.stderr)
        return 1
    print("tool manifest schema OK")
    return 0


def extract(archive: Path, destination: Path) -> None:
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            for member in zf.infolist():
                member_path = Path(member.filename)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise ValueError(f"unsafe ZIP member: {member.filename}")
            zf.extractall(destination)
    elif tarfile.is_tarfile(archive):
        with tarfile.open(archive) as tf:
            tf.extractall(destination, filter="data")
    else:
        raise ValueError(f"unsupported tool archive: {archive.name}")


def executable_for(item: dict, target: str) -> str:
    expected_suffix = ".exe" if target.startswith("win32-") else ""
    executable = item["executable"]
    if expected_suffix and not executable.endswith(expected_suffix):
        raise ValueError(f"Windows executable must end in .exe: {item['name']}")
    if not expected_suffix and executable.endswith(".exe"):
        raise ValueError(f"Linux executable must not end in .exe: {item['name']}")
    return executable


def staged_name_for(item: dict, target: str) -> str:
    """The file name the tool is staged (and looked up at runtime) under.

    ``trove.runtime.tool`` looks for a bare ``<name>``/``<name>.exe``,
    so an archive member with a decorated name must be renamed on the way in.
    """
    name = item.get("install_as") or executable_for(item, target)
    if target.startswith("win32-") and not name.endswith(".exe"):
        raise ValueError(f"Windows install_as must end in .exe: {item['name']}")
    if not target.startswith("win32-") and name.endswith(".exe"):
        raise ValueError(f"Linux install_as must not end in .exe: {item['name']}")
    return name


def archive_root(extracted: Path) -> Path:
    """The directory ``runtime_libs`` globs are resolved against.

    Both upstream archives wrap everything in a single versioned directory, so
    globbing the extraction directory itself would match nothing. Unwrap exactly
    one level, and only when that level is unambiguous -- an archive that spills
    its contents at the top is used as-is rather than guessed at.
    """
    entries = list(extracted.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return extracted


def stage_runtime_libs(item: dict, extracted: Path, stage_tmp: Path) -> list[str]:
    """Copy an executable's shared libraries flat into the stage directory.

    Flat, rather than mirroring the archive's ``bin/`` + ``lib/`` split, because
    that is the layout both platforms can actually load from: on Windows the
    loader searches the directory the .exe lives in, so the DLLs beside it are
    found with no help at all, and on Linux one ``LD_LIBRARY_PATH`` pointing at
    the staged directory covers everything (see ``trove.runtime.tool_env``).
    It also keeps ``runtime.tool``'s flat lookup unchanged.

    Symlinks are recreated as symlinks, never followed. ``lib/`` ships both
    ``libavcodec.so.62.28.102`` and a ``libavcodec.so.62`` soname link pointing at
    it; dereferencing would stage 90 MB twice for that library alone and undo the
    entire reason for using a shared build. The soname link is also the name
    recorded in the executable's DT_NEEDED, so it has to survive as a name.
    """
    patterns = item.get("runtime_libs")
    if not patterns:
        return []
    root = archive_root(extracted)
    staged_names: list[str] = []
    for pattern in patterns:
        matches = sorted(root.glob(pattern))
        if not matches:
            raise ValueError(f"runtime_libs pattern {pattern!r} matched nothing for {item['name']}")
        for source in matches:
            destination = stage_tmp / source.name
            # ffmpeg and ffprobe share one archive and one set of libraries, so
            # the second tool through here finds them already staged.
            if destination.exists() or destination.is_symlink():
                continue
            if source.is_symlink():
                link = os.readlink(source)
                # Flattening only preserves a link whose target is a bare name in
                # the same directory. Anything else would dangle, so copy the
                # real file instead of staging a broken link.
                if os.path.basename(link) == link:
                    os.symlink(link, destination)
                else:
                    shutil.copy2(source, destination, follow_symlinks=True)
            else:
                shutil.copy2(source, destination, follow_symlinks=False)
            staged_names.append(source.name)
    return staged_names


def can_probe(target: str) -> bool:
    """True when staged binaries can actually run on this host.

    Release CI stages each target natively, so the version probe runs there. A
    cross-target stage (inspecting the Windows payload from a Linux checkout) is
    still useful for verifying download, hash and layout, and simply records the
    probe as skipped.
    """
    host_win = sys.platform.startswith("win")
    return target.startswith("win32-") == host_win


def version_args(name: str) -> list[str]:
    return ["-ver"] if name == "exiftool" else ["-version"]


def probe_env(target: str, stage_tmp: Path) -> dict:
    """The environment the version probe runs a freshly staged binary under.

    Deliberately routed through the *application's* helper rather than setting
    ``LD_LIBRARY_PATH`` here: if staging and the app disagreed about how a bundled
    tool finds its libraries, the probe would pass on the build machine and the
    tool would fail on a user's. Pointing ``ARCHIVE_TOOLS_DIR`` at the staging
    directory is exactly what the desktop app does with the installed one.
    """
    base = dict(os.environ)
    base["ARCHIVE_TOOLS_DIR"] = str(stage_tmp)
    if target.startswith("win32-"):
        return base
    return runtime.tool_env(base)


def fetch_archive(item: dict, download_tmp: Path, archives: dict[tuple[str, str], Path]) -> Path:
    """Download one tool's archive and verify it, reusing an already-fetched one.

    Keyed on (url, digest) because several tools ship inside the same archive:
    ffmpeg and ffprobe come out of one download, and fetching it twice would
    double the build's network time for no gain. A digest mismatch raises here,
    before anything is extracted, so bad bytes never reach the stage directory.
    """
    key = (item["url"], item["sha256"].lower())
    archive = archives.get(key)
    if archive is not None:
        return archive
    archive = download_tmp / f"download-{len(archives)}"
    urllib.request.urlretrieve(item["url"], archive)
    actual_hash = sha256(archive).lower()
    if actual_hash != key[1]:
        raise ValueError(f"SHA-256 mismatch for {item['name']}: got {actual_hash}")
    archives[key] = archive
    return archive


def probe_version(item: dict, target: str, staged: Path, stage_tmp: Path) -> str:
    """Run the staged binary once and read the version it reports.

    This is the first thing that actually *runs* a staged binary, so it gets
    the same library path the app will. A shared build without that dies with
    "error while loading shared libraries" under check=True, which is the
    point: staging should fail here, on the build machine, rather than in a
    user's install. Cross-staging cannot run the binary at all, and says so
    rather than pretending it verified something.
    """
    if not can_probe(target):
        runtime_version = f"not probed (staged for {target} on {sys.platform})"
        print(
            f"warning: {item['name']} staged without a version probe ({runtime_version})",
            file=sys.stderr,
        )
        return runtime_version
    probe = subprocess.run(
        [str(staged), *version_args(item["name"])],
        check=True,
        text=True,
        capture_output=True,
        env=probe_env(target, stage_tmp),
    )
    return (probe.stdout or probe.stderr).splitlines()[0]


def stage_one(item: dict, target: str, stage_tmp: Path, extracted: Path) -> dict:
    """Copy one tool out of its extracted archive, and describe what was staged.

    Returns the tool's ``tools-build-info.json`` entry, which is the record of
    exactly what shipped: pinned digest, upstream url, and the version the
    binary itself reported when run.
    """
    executable = executable_for(item, target)
    hits = [path for path in extracted.rglob(executable) if path.is_file()]
    if len(hits) != 1:
        raise ValueError(f"expected one {executable} in {item['name']} archive, found {len(hits)}")
    staged = stage_tmp / staged_name_for(item, target)
    shutil.copy2(hits[0], staged)
    if target.startswith("linux-"):
        staged.chmod(staged.stat().st_mode | 0o111)
    support = item.get("support_dir")
    if support:
        # The runtime tree ships beside the executable inside the archive
        # and must keep that relative position once staged.
        source_dir = hits[0].parent / support
        if not source_dir.is_dir():
            raise ValueError(f"missing support_dir {support!r} for {item['name']}")
        destination_dir = stage_tmp / support
        if not destination_dir.exists():
            shutil.copytree(source_dir, destination_dir)
    staged_libs = stage_runtime_libs(item, extracted, stage_tmp)
    return {
        "name": item["name"],
        "version": item["version"],
        "url": item["url"],
        "sha256": item["sha256"].lower(),
        "license": item["license"],
        "runtime_version": probe_version(item, target, staged, stage_tmp),
        "runtime_libs": staged_libs,
    }


def stage_target(target: str) -> Path:
    tools, unavailable = validate_target(target)
    final_stage = ROOT / "packaging" / "tools" / "staged" / target
    final_stage.parent.mkdir(parents=True, exist_ok=True)
    with (
        tempfile.TemporaryDirectory(prefix=f".{target}-", dir=final_stage.parent) as stage_tmp_name,
        tempfile.TemporaryDirectory(prefix="archive-tools-") as download_tmp_name,
    ):
        stage_tmp, download_tmp = Path(stage_tmp_name), Path(download_tmp_name)
        archives: dict[tuple[str, str], Path] = {}
        build_info: list[dict] = []
        for item in tools:
            archive = fetch_archive(item, download_tmp, archives)
            extracted = download_tmp / f"extract-{item['name']}"
            extracted.mkdir()
            extract(archive, extracted)
            build_info.append(stage_one(item, target, stage_tmp, extracted))
        info = {"target": target, "tools": build_info, "unavailable": unavailable}
        (stage_tmp / "tools-build-info.json").write_text(
            json.dumps(info, indent=2) + "\n", encoding="utf-8"
        )
        if final_stage.exists():
            shutil.rmtree(final_stage)
        os.replace(stage_tmp, final_stage)
        return final_stage


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", help="e.g. win32-x64 or linux-x64")
    parser.add_argument("--validate", action="store_true")
    args = parser.parse_args()
    if args.validate:
        return validate()
    if not args.target:
        parser.error("--target is required unless --validate is used")
    try:
        print(stage_target(args.target))
    except (OSError, ValueError, subprocess.CalledProcessError, urllib.error.URLError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
