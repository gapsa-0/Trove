"""What keeps this tier from filling the disk.

The fixture leaked about 140 MB per session, and the reason nobody caught it
for 171 sessions is the reason these tests exist: every part of the old
teardown reported success. `TemporaryDirectory` deleted a directory and found
nothing wrong with it being empty, because under snap confinement the browser
had written its real profile into a private-namespace copy of the same path --
in a tree root-owned 0700, where `du` as a normal user reports nothing at all.
24 GB later the root filesystem was at 99%.

So these assert the outcome rather than the mechanism: after a completed launch
and teardown, no `trove-cdp-*` directory is left anywhere this tier writes, and
the profile the browser used was one this process could actually see.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import profiles
import pytest
from conftest import _CHROME_BINARIES, _endpoint_ready


def _existing_profiles(snap: str | None) -> set[Path]:
    found: set[Path] = set()
    for root in profiles.profile_roots(snap):
        found.update(root.glob(profiles.PROFILE_PREFIX + "*"))
    return found


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Launch:
    """What one full launch/teardown cycle looked like from outside."""

    def __init__(self):
        self.profile: Path | None = None
        self.was_written: bool = False
        self.pgid: int | None = None
        self.before: set[Path] = set()
        self.after: set[Path] = set()


@pytest.fixture(scope="module")
def launched() -> Launch:
    """Run the fixture's own launch and teardown once, and record what happened.

    Deliberately not the `cdp_port` fixture itself: what is under test here is
    its teardown, and a session-scoped fixture is still holding its browser open
    while these tests run. This drives the same helpers through the same
    sequence, then observes the disk afterwards.
    """
    binary = next((b for b in _CHROME_BINARIES if shutil.which(b)), None)
    if binary is None:
        pytest.skip("no Chrome/Chromium on PATH")

    seen = Launch()
    snap = profiles.snap_confinement(binary)
    seen.before = _existing_profiles(snap)
    port = _free_port()

    with profiles.browser_profile(snap) as profile:
        seen.profile = profile
        proc = subprocess.Popen(
            [
                binary,
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                f"--remote-debugging-port={port}",
                "--remote-debugging-address=127.0.0.1",
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--disable-extensions",
                "--disable-background-networking",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            if not _endpoint_ready(port):
                proc.kill()
                pytest.skip(f"{binary} did not open a debugging port within 20s")
            seen.was_written = profiles.profile_is_real(profile)
            seen.pgid = os.getpgid(proc.pid)
        finally:
            profiles.stop_browser(proc)

    seen.after = _existing_profiles(snap)
    return seen


def test_no_profile_survives_a_completed_session(launched):
    """The regression itself: nothing is left behind, in either location.

    Both roots are checked, not just the one this machine's browser uses, so
    the test still fails if a change sends the profile back to /tmp on a
    machine where /tmp is the wrong answer.
    """
    assert launched.after == launched.before, (
        f"profiles left behind: {sorted(launched.after - launched.before)}"
    )
    assert not launched.profile.exists()


def test_the_profile_was_somewhere_this_process_can_see(launched):
    """The other half, and the half that was silently false before.

    An empty profile directory next to a browser that answered on its debugging
    port is not a clean run -- it means the real profile went somewhere else,
    which is precisely the state the old fixture deleted and called done.
    """
    assert launched.was_written, (
        f"{launched.profile} was empty while the browser was running: its real "
        "profile went into a namespace this process cannot clean up"
    )


def test_the_browser_leaves_no_children_behind(launched):
    """`terminate()` alone left the crashpad handler writing into the profile.

    Signalling the group is what makes the deletion above safe rather than a
    race, so the group being empty is worth asserting on its own -- a partial
    teardown would otherwise show up only as an occasional dirty rmtree.
    """
    assert launched.pgid is not None
    with pytest.raises(OSError):
        os.killpg(launched.pgid, 0)


def test_stale_profiles_are_swept_and_live_ones_are_not(tmp_path, monkeypatch):
    """A session killed outright never reaches teardown, so a sweep is the floor.

    The cutoff matters in both directions: a sweep that spared nothing would
    delete the profile of a run happening concurrently.
    """
    monkeypatch.setattr(profiles, "profile_roots", lambda snap: [tmp_path])
    stale = tmp_path / f"{profiles.PROFILE_PREFIX}old"
    fresh = tmp_path / f"{profiles.PROFILE_PREFIX}running"
    unrelated = tmp_path / "someone-elses-tmpdir"
    for path in (stale, fresh, unrelated):
        path.mkdir()
        (path / "Local State").write_text("{}")
    old = time.time() - profiles.STALE_PROFILE_AGE - 60
    os.utime(stale, (old, old))
    os.utime(unrelated, (old, old))

    removed = profiles.sweep_stale_profiles(None)

    assert removed == [stale]
    assert not stale.exists()
    assert fresh.exists(), "a profile younger than the cutoff may still be in use"
    assert unrelated.exists(), "the sweep must only ever touch its own prefix"


@pytest.mark.parametrize(
    "resolved",
    [
        "/snap/bin/chromium",
        "/snap/chromium/current/usr/lib/chromium/chrome",
    ],
)
def test_snap_paths_are_recognised(monkeypatch, resolved):
    monkeypatch.setattr(profiles.shutil, "which", lambda binary: resolved)
    assert profiles.snap_confinement("chromium") == "chromium"


def test_the_ubuntu_shim_is_recognised(tmp_path, monkeypatch):
    """/usr/bin/chromium-browser is a shell script with nothing in its own path.

    It execs the snap, so a profile it is handed lands in the private namespace
    exactly as `chromium` would -- but every path check on the shim itself says
    "ordinary binary". Reading it is the only way to tell.
    """
    shim = tmp_path / "chromium-browser"
    shim.write_text('#!/bin/sh\nexec /snap/bin/chromium "$@"\n')
    monkeypatch.setattr(profiles.shutil, "which", lambda binary: str(shim))
    assert profiles.snap_confinement("chromium-browser") == "chromium"


def test_an_ordinary_binary_is_not_confined(tmp_path, monkeypatch):
    plain = tmp_path / "google-chrome"
    plain.write_bytes(b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 64)
    monkeypatch.setattr(profiles.shutil, "which", lambda binary: str(plain))
    assert profiles.snap_confinement("google-chrome") is None
    assert profiles.profile_root(None) is None


def test_a_confined_browser_is_never_given_a_tmp_profile():
    """The fix in one line: under a snap, nothing this tier writes is under /tmp.

    Including pytest's own `tmp_path`, whose basetemp defaults there -- the
    remap applies to the whole of /tmp, not to `mkdtemp` specifically.
    """
    root = profiles.profile_root("chromium")
    assert root is not None
    assert Path("/tmp") not in root.parents
    assert root == Path.home() / "snap" / "chromium" / "common"
