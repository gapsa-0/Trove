"""Noticing that files arrived, without waiting for the next poll.

This module is a *hint*, and everything about its design follows from that.
Whether an archive is up to date is answered by ``stages._pending`` walking the
tree and comparing what it finds against the last completed scan; that answer is
correct on every filesystem, and it is not made here. All this does is tell the
scheduler to ask sooner than its backoff would have.

That is what makes it safe to ship against filesystems where watching does not
work. inotify does not deliver events for network mounts and is unreliable on
FUSE; it also needs one watch per directory out of a per-user budget that a
large archive can exhaust. macOS and Windows cover a whole tree from a single
handle but coalesce events under load, and any of the three can drop them.
Every one of those failures degrades to the poll's own timing -- the behaviour
before this module existed -- rather than to a file that never gets read.

``watchfiles`` is an optional dependency for the same reason. Not installed
means no hint, which is a slower app and not a broken one, so nothing here may
be imported at module scope by code that must run without it.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)

# How long to let a burst of arrivals settle before telling the scheduler. Sized
# for photographs, which is what arrives in bursts: a folder of them lands in
# one go and this only has to outlast the gaps between individual files. It is
# not the wait for a large video to finish copying -- that would need a window
# no one wants to sit through, and is the walker's job instead (see
# scan.walker._still_arriving).
DEBOUNCE = 1.5

# The floor between two hints acted on for the same archive. Acting on a hint
# costs a full walk of the tree, so a short debounce alone would turn files
# arriving one at a time into a walk each. Responsiveness and how often we are
# willing to walk are separate questions and get separate numbers; a hint that
# lands inside the floor is not dropped, it fires once the floor has passed.
WALK_FLOOR = 12.0


def available() -> bool:
    """Whether filesystem events can be watched at all in this installation."""
    try:
        import watchfiles  # noqa: F401
    except ImportError:
        return False
    return True


class ArchiveWatcher:
    """Watches one archive's folder and calls ``on_change`` when it changes.

    One archive at a time, matching the rule that only the open archive is
    scheduled -- which also bounds the inotify watches held to a single tree.
    ``start`` on an archive already being watched is a no-op, so the caller may
    treat it as "make sure this one is the watched one".
    """

    def __init__(self, on_change: Callable[[int], None]):
        self._on_change = on_change
        self._root_id: int | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def start(self, root_id: int, path: str) -> None:
        with self._lock:
            if self._root_id == root_id and self._thread is not None:
                return
        self.stop()
        if not available():
            logger.debug("watch: watchfiles is not installed, polling only")
            return
        with self._lock:
            self._stop = threading.Event()
            self._root_id = root_id
            self._thread = threading.Thread(
                target=self._watch,
                args=(root_id, path, self._stop),
                name=f"watch-{root_id}",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            thread, self._thread, self._root_id = self._thread, None, None
            self._stop.set()
        if thread is not None:
            # Not joined. watchfiles wakes on its own timeout to check the stop
            # event, so a join here would make closing an archive wait out that
            # timeout for nothing; the thread is a daemon and its only remaining
            # act is to notice the event and return.
            logger.debug("watch: stopped")

    def _watch(self, root_id: int, path: str, stop: threading.Event) -> None:
        """The watch thread. Every failure here is logged and ends the thread:
        the poll is still running, so there is nothing to retry into."""
        import watchfiles

        try:
            for _changes in watchfiles.watch(
                path,
                # watch_filter is left at its default, which drops editor and
                # system litter (.DS_Store and friends). Nothing it hides is a
                # file this archive would have catalogued, and every event it
                # hides is a tree walk saved.
                debounce=int(DEBOUNCE * 1000),
                step=50,
                stop_event=stop,
                recursive=True,
                # A drive being unplugged is a normal thing to happen, and the
                # card that says "not mounted" is already the answer to it.
                ignore_permission_denied=True,
                # Come up for air every second so closing an archive is not
                # waiting out a five-second timeout to see the stop event.
                rust_timeout=1000,
                yield_on_timeout=False,
                # Only the main thread ever receives one, and this is not it.
                raise_interrupt=False,
            ):
                if stop.is_set():
                    return
                self._on_change(root_id)
        except Exception:
            # Broad on purpose, and the reason this is a hint: an inotify budget
            # that ran out mid-run, a mount that went away, a platform backend
            # refusing the folder. None of them are worth failing an archive
            # over when the poll still answers the question correctly.
            logger.warning("watch: stopped watching root=%s", root_id, exc_info=True)
