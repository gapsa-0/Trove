import json
import os
import signal
import subprocess
import sys
import time
from urllib.request import urlopen

import pytest

import organize_archive

# The only tests that start a real interpreter. Both spawn the backend as a
# subprocess, so they pay a full process start plus its imports, and they are
# the suite's one known flake (a SIGSEGV on SIGTERM, seen on CI and once here).
pytestmark = pytest.mark.slow


def _read_ready(process: subprocess.Popen) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        line = process.stdout.readline()
        if line.startswith("READY "):
            return json.loads(line[6:])
    raise AssertionError("desktop backend did not emit a readiness record")


def test_desktop_backend_readiness_health_and_shutdown(tmp_path):
    env = os.environ | {"XDG_DATA_HOME": str(tmp_path / "data")}
    process = subprocess.Popen(
        [sys.executable, "-m", "organize_archive.desktop", "--host", "127.0.0.1", "--port", "0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        ready = _read_ready(process)
        assert ready["port"] > 0
        with urlopen(f"http://127.0.0.1:{ready['port']}/api/health") as response:
            # Read the version rather than repeating it: the release process bumps
            # it in several files at once, and a copy here just breaks on release.
            assert json.load(response) == {
                "ok": True,
                "version": organize_archive.__version__,
                "commit": "dev",
            }
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.kill()


def test_desktop_backend_rejects_non_loopback_host():
    process = subprocess.run(
        [sys.executable, "-m", "organize_archive.desktop", "--host", "0.0.0.0"],
        capture_output=True,
        text=True,
    )
    assert process.returncode == 2
    assert process.stdout == ""
    assert "127.0.0.1" in process.stderr
