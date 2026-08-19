"""Where the browser's profile goes, and how it is guaranteed to be deleted.

Its own module rather than more of conftest.py because the answer is not one
line and the reasoning behind it is not obvious, and because the regression
test that guards it has to drive the same code the fixture does.

The problem it exists for: this tier leaked about 140 MB of disk per session
and did it invisibly, because under snap confinement the directory the browser
wrote and the directory Python deleted were two different directories with the
same path. 171 sessions put 24 GB into a place `du` cannot see and took a root
filesystem to 99% full. `snap_confinement` and `profile_root` are what stops
that happening again; `profile_is_real` is what stops a *different* remap doing
it silently; `sweep_stale_profiles` is what bounds the case where the process
dies before it can clean up at all.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from contextlib import contextmanager, suppress
from pathlib import Path

# Every profile this tier creates is named so it can be recognised later by a
# sweep -- including a sweep run by a different session than made it.
PROFILE_PREFIX = "trove-cdp-"

# How long a leftover profile is allowed to sit before another session removes
# it. Generous on purpose: the sweep has no way to ask whether a concurrent run
# is still using one, so the only thing keeping it from deleting a live profile
# is that no run of this tier lasts hours -- the whole tier is under two
# minutes. Four hours is far outside that and still bounds the worst case.
STALE_PROFILE_AGE = 4 * 3600


def _snap_name(path: Path) -> str | None:
    """The snap a path belongs to: ``/snap/bin/chromium`` -> ``chromium``."""
    parts = path.parts
    if len(parts) < 3 or parts[1] != "snap":
        return None
    if parts[2] == "bin":
        return parts[3] if len(parts) > 3 else None
    return parts[2]


def snap_confinement(binary: str) -> str | None:
    """The snap ``binary`` runs inside, or ``None`` for an ordinary binary.

    Worth detecting because confinement silently changes what a path *means*. A
    snap runs in its own mount namespace where ``/tmp`` is a private bind mount
    over ``/tmp/snap-private-tmp/snap.<name>/tmp``, so one profile path under
    ``/tmp`` names two directories: Python creates and later deletes the host
    one, which stays empty and cleans up without complaint, while the browser
    writes the real 140 MB profile into the namespace copy, which nothing ever
    removes and no ordinary user can even list (that tree is root-owned 0700).

    Detection has to look in two places, because on Ubuntu both candidates in
    ``_CHROME_BINARIES`` reach the same snap by different routes:
    ``/snap/bin/chromium`` is a symlink to ``/usr/bin/snap``, so the *link*
    path names the snap and the resolved target does not, and
    ``/usr/bin/chromium-browser`` is a plain shell shim that execs
    ``/snap/bin/chromium`` with nothing in its own path to give it away.
    """
    which = shutil.which(binary)
    if which is None:
        return None
    for candidate in (Path(which), Path(which).resolve()):
        name = _snap_name(candidate)
        if name is not None:
            return name
    try:
        with open(which, "rb") as handle:
            head = handle.read(4096)
    except OSError:
        return None
    if not head.startswith(b"#!"):
        return None
    match = re.search(rb"/snap/bin/([\w.+-]+)", head)
    return match.group(1).decode() if match else None


def profile_root(snap: str | None) -> Path | None:
    """Where to create the profile -- ``None`` meaning the ordinary temp dir.

    Under a snap the answer has to be a path both namespaces agree on, which
    rules out the whole of ``/tmp``. That includes pytest's ``tmp_path``: its
    basetemp defaults under ``/tmp`` too, so the fixture that looks like the
    obvious home for this is exactly as unsafe as ``mkdtemp``.

    ``~/snap/<name>/common`` is the snap's own writable area and confinement
    leaves it at its real host path, so the profile the browser writes is the
    profile this process can delete. A hidden directory in ``$HOME`` would not
    work in its place: the ``home`` interface does not expose dot-directories
    to the snap at all.
    """
    if snap is None:
        return None
    root = Path.home() / "snap" / snap / "common"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Nothing writable to offer. Falling back to the temp dir does not
        # reintroduce the silent leak -- `profile_is_real` refuses to run on a
        # profile the browser did not actually write into.
        return None
    return root


def profile_roots(snap: str | None) -> list[Path]:
    """Every directory this tier may have left a profile in for this browser.

    The ordinary temp dir is on the list whatever the answer to confinement is,
    because that is where every session before this fix put its profile, and on
    an unconfined machine it is still the right place. What is never on the list
    is the private-namespace copy: an unprivileged sweep cannot reach it, so a
    profile that lands there is unrecoverable rather than merely stale.
    """
    roots = [Path(tempfile.gettempdir())]
    root = profile_root(snap)
    if root is not None and root not in roots:
        roots.append(root)
    return roots


def sweep_stale_profiles(snap: str | None) -> list[Path]:
    """Delete profiles older than `STALE_PROFILE_AGE`; return what was removed.

    Teardown removes the profile of a session that ends normally. A session
    killed outright -- a doubled ^C, a CI timeout, an OOM kill -- never reaches
    teardown, and nothing else on the machine knows what these directories are.
    A sweep is therefore the only thing that bounds the worst case: without it
    the growth has no limit at all, with it the limit is however many sessions
    start inside the window.

    The namespace-private copies under ``/tmp/snap-private-tmp`` cannot be
    swept, whatever their age: that tree is root-owned 0700, so an
    unprivileged process cannot list it, let alone delete from it. Not writing
    there in the first place is the only repair available, which is what
    `profile_root` is for.
    """
    cutoff = time.time() - STALE_PROFILE_AGE
    removed = []
    for root in profile_roots(snap):
        try:
            candidates = sorted(root.glob(PROFILE_PREFIX + "*"))
        except OSError:
            continue
        for path in candidates:
            try:
                if not path.is_dir() or path.stat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue
            shutil.rmtree(path, ignore_errors=True)
            if not path.exists():
                removed.append(path)
    return removed


@contextmanager
def browser_profile(snap: str | None):
    """A profile directory, deleted once the browser has finished with it.

    Deliberately not `tempfile.TemporaryDirectory`: that reports success
    whether or not the directory it removed was the one anybody wrote to, which
    is precisely how the original leak stayed invisible for 171 sessions.
    """
    profile = Path(tempfile.mkdtemp(prefix=PROFILE_PREFIX, dir=profile_root(snap)))
    try:
        yield profile
    finally:
        shutil.rmtree(profile, ignore_errors=True)


def profile_is_real(profile: Path, timeout: float = 5.0) -> bool:
    """Whether the browser actually wrote into the directory it was handed.

    The check that makes `snap_confinement` non-critical. Detection is a
    heuristic about someone else's packaging, and the failure mode when it
    guesses wrong is the silent unbounded one. This observes the outcome
    instead: a browser answering on its debugging port has long since written
    ``Local State`` and ``Default`` into its profile, so an empty directory at
    that point means the real one went somewhere in a namespace this process
    can neither see nor delete. Callers turn that into a loud skip rather than
    another 140 MB nobody finds for months.
    """
    deadline = time.monotonic() + timeout
    while True:
        with suppress(OSError):
            if any(profile.iterdir()):
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _group_gone(pgid: int, timeout: float) -> bool:
    """Whether every process in the group has exited, waiting up to *timeout*."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.killpg(pgid, 0)
        except OSError:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def stop_browser(proc: subprocess.Popen) -> None:
    """Stop the browser *and* its children, and wait for all of them to go.

    ``proc.terminate()`` signals the launcher alone, and the launcher is not
    where the writing happens: ``--headless=new`` runs a crashpad handler, a
    zygote and a renderer per tab as separate processes -- ten of them, counted
    on this machine -- any of which can still be writing into the profile while
    the ``rmtree`` after it runs. That race is why the browser is started with
    ``start_new_session``: it puts the whole tree in one process group that can
    be signalled, and waited for, as a unit.

    The direct child is reaped before the group is polled. Until it is, its own
    zombie keeps the group alive and the poll would report stragglers that have
    already exited.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None

    if pgid is None:
        proc.terminate()
    else:
        with suppress(OSError):
            os.killpg(pgid, signal.SIGTERM)

    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if pgid is None:
            proc.kill()
        else:
            with suppress(OSError):
                os.killpg(pgid, signal.SIGKILL)
        with suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)

    if pgid is None or _group_gone(pgid, timeout=5.0):
        return
    with suppress(OSError):
        os.killpg(pgid, signal.SIGKILL)
    _group_gone(pgid, timeout=5.0)
