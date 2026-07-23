#!/usr/bin/env python3
"""Fetch and verify a target-specific native-tool release.

This never accepts an unchecked download.  The public manifest is intentionally
empty until distribution sources, licences and SHA-256 values are approved.
"""
from __future__ import annotations

import argparse, hashlib, json, os, shutil, subprocess, sys, tempfile, urllib.request, zipfile, tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "packaging" / "tools" / "manifest.json"

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def load_target(target: str) -> list[dict]:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("targets"), dict):
        raise SystemExit("invalid packaging/tools/manifest.json schema")
    tools = data["targets"].get(target)
    if not tools:
        raise SystemExit(f"no approved tool payload for {target}; add URL, SHA-256 and licence first")
    for item in tools:
        required = ("name", "version", "url", "sha256", "license", "executable")
        if not all(isinstance(item.get(key), str) and item[key] for key in required):
            raise SystemExit(f"invalid tool entry for {target}: required fields are {', '.join(required)}")
    return tools

def validate() -> int:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("targets"), dict):
        print("invalid manifest", file=sys.stderr); return 1
    for target in data["targets"]:
        load_target(target)
    print("tool manifest schema OK")
    return 0

def extract(archive: Path, destination: Path) -> None:
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z: z.extractall(destination)
    elif tarfile.is_tarfile(archive):
        with tarfile.open(archive) as t: t.extractall(destination, filter="data")
    else:
        shutil.copy2(archive, destination / archive.name)

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", help="e.g. win32-x64 or linux-x64")
    parser.add_argument("--validate", action="store_true")
    args = parser.parse_args()
    if args.validate: return validate()
    if not args.target: parser.error("--target is required unless --validate is used")
    tools = load_target(args.target)
    stage = ROOT / "packaging" / "tools" / "staged" / args.target
    if stage.exists(): shutil.rmtree(stage)
    stage.mkdir(parents=True)
    with tempfile.TemporaryDirectory() as tmp:
        temp = Path(tmp)
        for item in tools:
            archive = temp / f"{item['name']}.download"
            urllib.request.urlretrieve(item["url"], archive)
            if sha256(archive).lower() != item["sha256"].lower():
                raise SystemExit(f"SHA-256 mismatch for {item['name']}")
            out = temp / item["name"]; out.mkdir()
            extract(archive, out)
            hits = list(out.rglob(item["executable"]))
            if len(hits) != 1: raise SystemExit(f"expected one {item['executable']} in {item['name']} archive")
            shutil.copy2(hits[0], stage / item["executable"])
    for executable in stage.iterdir():
        executable.chmod(executable.stat().st_mode | 0o111)
    commands = {"exiftool": ["-ver"], "ffprobe": ["-version"], "ffmpeg": ["-version"]}
    for name, command_args in commands.items():
        executable = stage / (name + (".exe" if args.target.startswith("win32-") else ""))
        if not executable.is_file(): raise SystemExit(f"staged payload is missing required {executable.name}")
        subprocess.run([str(executable), *command_args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(stage)
    return 0

if __name__ == "__main__": raise SystemExit(main())
