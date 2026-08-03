"""Staging a shared-build tool, and letting it find its own libraries.

FFmpeg used to be staged from a *static* build: two self-contained binaries,
``ffmpeg`` and ``ffprobe``, each carrying the entire codec set. That was 266 MB
to ship one copy of FFmpeg twice. The shared build is 162 MB -- small binaries
beside the ``libav*`` libraries they share -- but it only works if two things
hold, and neither is obvious enough to leave unasserted:

* the soname symlinks survive staging. ``lib/`` ships ``libavcodec.so.61.19.101``
  and a ``libavcodec.so.61`` link to it, and ``libavcodec.so.61`` is the name in
  the executable's DT_NEEDED. Following the links instead of recreating them
  stages 90 MB twice for that one library and gives back everything the change
  saved -- while still passing every other test, because the binary still runs.
* the process that spawns the tool passes a library path. Upstream's RPATH is
  ``-Wl:../lib``, a quoting bug in BtbN's link flags rather than the intended
  ``$ORIGIN/../lib``, so nothing resolves on its own.

The staging script is loaded by path: ``packaging/scripts/stage-tools.py`` is a
script, not an importable module, and it is not on sys.path.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from organize_archive import runtime

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "packaging" / "scripts" / "stage-tools.py"


@pytest.fixture(scope="module")
def stage_tools():
    spec = importlib.util.spec_from_file_location("stage_tools", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def archive(tmp_path):
    """An extraction directory shaped like the shared FFmpeg tarball."""
    root = tmp_path / "extracted" / "ffmpeg-n7.1.5-linux64-gpl-shared-7.1"
    (root / "bin").mkdir(parents=True)
    (root / "lib").mkdir()
    (root / "bin" / "ffmpeg").write_bytes(b"#!/bin/false\n")
    (root / "lib" / "libavcodec.so.61.19.101").write_bytes(b"x" * 4096)
    (root / "lib" / "libavcodec.so.61").symlink_to("libavcodec.so.61.19.101")
    # A .lib import library and a pkgconfig tree sit in the same directory in the
    # real archive; the glob must not sweep them in.
    (root / "lib" / "libavcodec.a").write_bytes(b"static")
    return tmp_path / "extracted"


def test_libraries_are_staged_flat_beside_the_executable(stage_tools, archive, tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()

    staged = stage_tools.stage_runtime_libs(
        {"name": "ffmpeg", "runtime_libs": ["lib/lib*.so.*"]}, archive, stage
    )

    assert sorted(staged) == ["libavcodec.so.61", "libavcodec.so.61.19.101"]
    assert not (stage / "libavcodec.a").exists()
    assert sorted(p.name for p in stage.iterdir()) == [
        "libavcodec.so.61",
        "libavcodec.so.61.19.101",
    ]


def test_the_soname_symlink_is_recreated_not_followed(stage_tools, archive, tmp_path):
    """The whole size saving rides on this: a followed link doubles the payload."""
    stage = tmp_path / "stage"
    stage.mkdir()

    stage_tools.stage_runtime_libs(
        {"name": "ffmpeg", "runtime_libs": ["lib/lib*.so.*"]}, archive, stage
    )

    link = stage / "libavcodec.so.61"
    assert link.is_symlink(), "staged as a copy; the shared build is now 2x its size"
    assert os.readlink(link) == "libavcodec.so.61.19.101"
    assert link.resolve() == (stage / "libavcodec.so.61.19.101").resolve()


def test_a_second_tool_sharing_the_archive_stages_nothing_twice(stage_tools, archive, tmp_path):
    """ffmpeg and ffprobe come out of one archive and share one set of libraries."""
    stage = tmp_path / "stage"
    stage.mkdir()
    item = {"name": "ffmpeg", "runtime_libs": ["lib/lib*.so.*"]}

    first = stage_tools.stage_runtime_libs(item, archive, stage)
    second = stage_tools.stage_runtime_libs({**item, "name": "ffprobe"}, archive, stage)

    assert first and second == []


def test_a_pattern_that_matches_nothing_fails_the_build(stage_tools, archive, tmp_path):
    """Silently staging no libraries would ship a tool that cannot start."""
    stage = tmp_path / "stage"
    stage.mkdir()

    with pytest.raises(ValueError, match="matched nothing"):
        stage_tools.stage_runtime_libs(
            {"name": "ffmpeg", "runtime_libs": ["lib/nosuch*.so"]}, archive, stage
        )


def test_runtime_libs_must_stay_inside_the_archive(stage_tools, monkeypatch, tmp_path):
    """The globs come from a manifest; treat them like every other path input."""

    def entry(name, **extra):
        return {
            "name": name,
            "version": "v",
            "url": "https://x/a.tar.xz",
            "sha256": "a" * 64,
            "license": "GPL",
            "executable": name,
            **extra,
        }

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "targets": {
                    "linux-x64": {
                        "tools": [
                            entry("ffmpeg", runtime_libs=["../../etc/*.so"]),
                            entry("ffprobe"),
                        ],
                        "unavailable": [],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(stage_tools, "MANIFEST", manifest)

    with pytest.raises(ValueError, match="runtime_libs must be relative"):
        stage_tools.validate_target("linux-x64")


def test_the_repos_own_tool_manifest_is_valid(stage_tools):
    assert stage_tools.validate() == 0


def test_the_shipped_manifest_asks_for_ffmpegs_libraries(stage_tools):
    """A shared build with no runtime_libs stages binaries that cannot start."""
    tools, _ = stage_tools.target_data("linux-x64")
    by_name = {tool["name"]: tool for tool in tools}
    assert by_name["ffmpeg"]["runtime_libs"] == ["lib/lib*.so.*"]

    tools, _ = stage_tools.target_data("win32-x64")
    by_name = {tool["name"]: tool for tool in tools}
    assert by_name["ffmpeg"]["runtime_libs"] == ["bin/*.dll"]
    # ExifTool is a self-contained .exe plus its Perl tree; it needs none.
    assert "runtime_libs" not in by_name["exiftool"]


# --- runtime.tool_env ------------------------------------------------------


def test_tool_env_points_the_loader_at_the_staged_directory(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("ARCHIVE_TOOLS_DIR", "/opt/trove/tools")
    monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)

    assert runtime.tool_env()["LD_LIBRARY_PATH"] == "/opt/trove/tools"


def test_tool_env_prepends_rather_than_appends(monkeypatch):
    """A frozen build inherits PyInstaller's _internal on LD_LIBRARY_PATH.

    That directory carries OpenCV's own bundled libav*. Appending would let a
    foreign libavcodec answer ffmpeg's DT_NEEDED first, which is the kind of
    failure that shows up as a codec quietly not working.
    """
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("ARCHIVE_TOOLS_DIR", "/opt/trove/tools")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/app/_internal")

    assert runtime.tool_env()["LD_LIBRARY_PATH"] == f"/opt/trove/tools{os.pathsep}/app/_internal"


def test_tool_env_leaves_windows_alone(monkeypatch):
    """Windows resolves a DLL from the .exe's own directory; nothing to set."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("ARCHIVE_TOOLS_DIR", r"C:\Trove\tools")
    monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)

    assert "LD_LIBRARY_PATH" not in runtime.tool_env()


def test_tool_env_is_a_no_op_without_a_bundled_tools_directory(monkeypatch):
    """A source checkout uses ffmpeg from PATH, which needs no help."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("ARCHIVE_TOOLS_DIR", raising=False)
    monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)

    assert "LD_LIBRARY_PATH" not in runtime.tool_env()


def test_tool_env_resolves_roots_from_the_environment_it_is_given(monkeypatch):
    """stage-tools.py probes a directory this process has never run from."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("ARCHIVE_TOOLS_DIR", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)

    env = runtime.tool_env({"ARCHIVE_TOOLS_DIR": "/tmp/stage"})

    assert env["LD_LIBRARY_PATH"] == "/tmp/stage"
