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

**Why the watch is placed one directory at a time.** inotify has no recursive
mode: something has to enumerate the tree and register each directory, and
asking ``watchfiles`` to do it (``recursive=True``) means asking Rust to, which
it does *with the GIL held for the whole call* -- it stats every entry to find
the directories, 97k of them to reach 595 on this archive. Measured on the
97k-file archive: one unbroken 11.5 s freeze warm, 24-26 s cold, only 23% of it
CPU. Held GIL means no Python runs, so those seconds are not a slow app but a
stopped one -- every request, thumbnail and screen, on any disk, waits out all
of it. Enumerating the same directories here (``walker.iter_dirs``, where every
``scandir`` releases the GIL) and handing Rust an explicit non-recursive list
costs ~0.3 s of walking plus ~0.08 s to register, and stalls nothing.

The one thing a non-recursive watch does not do for free is cover directories
created *after* it was placed. Their parent is watched, so their arrival is
reported, and ``_watch_pass`` returns to re-enumerate -- which is affordable
precisely because placing the watch is no longer expensive.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ..scan.walker import iter_dirs

if TYPE_CHECKING:
    # From the submodule, not the package: watchfiles re-exports the name in
    # __all__ but binds it lazily, so the package has no such attribute to
    # resolve at type-check time.
    from watchfiles.main import FileChange

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


def _added_directory(batch: set[FileChange]) -> bool:
    """Whether this batch contains a directory the watch does not yet cover.

    Checked by stat rather than taken from the event, because inotify reports
    the creation of a file and of a directory the same way. It is a stat over
    the handful of paths in one debounced batch, not over the tree.

    A directory that has already been removed again reads as not-a-directory
    and no re-place happens, which is the right answer: there is nothing left
    to watch, and the walk this batch has already triggered is what decides
    whether anything about the archive actually changed.
    """
    from watchfiles import Change

    return any(change is Change.added and Path(p).is_dir() for change, p in batch)


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
        the poll is still running, so there is nothing to retry into.

        One pass per directory list. A pass ends when a directory appears that
        the current list does not cover, and the next one is placed over the
        tree as it now stands.
        """
        try:
            while not stop.is_set():
                if not self._watch_pass(root_id, path, stop):
                    return
        except Exception:
            # Broad on purpose, and the reason this is a hint: an inotify budget
            # that ran out mid-run, a mount that went away, a platform backend
            # refusing the folder. None of them are worth failing an archive
            # over when the poll still answers the question correctly.
            logger.warning("watch: stopped watching root=%s", root_id, exc_info=True)

    def _watch_pass(self, root_id: int, path: str, stop: threading.Event) -> bool:
        """Watch this tree's directories until the list needs rebuilding.

        True means "a directory arrived, place the watch again"; False means
        there is nothing left to watch and the thread is done.
        """
        import watchfiles

        if not Path(path).is_dir():
            # An unplugged drive, or a folder removed since the archive was
            # opened. The poll already reports that as "not mounted".
            logger.debug("watch: %s is not a folder, polling only", path)
            return False
        dirs = [str(d) for d in iter_dirs(Path(path))]
        logger.debug("watch: watching %d directories under root=%s", len(dirs), root_id)
        changes = watchfiles.watch(
            *dirs,
            # watch_filter is left at its default, which drops editor and
            # system litter (.DS_Store and friends). Nothing it hides is a
            # file this archive would have catalogued, and every event it
            # hides is a tree walk saved.
            debounce=int(DEBOUNCE * 1000),
            step=50,
            stop_event=stop,
            # Not recursive: this list is already every directory, and letting
            # Rust rediscover it freezes the app. See the module docstring.
            recursive=False,
            # A drive being unplugged is a normal thing to happen, and the
            # card that says "not mounted" is already the answer to it.
            ignore_permission_denied=True,
            # Come up for air every second so closing an archive is not
            # waiting out a five-second timeout to see the stop event.
            rust_timeout=1000,
            yield_on_timeout=False,
            # Only the main thread ever receives one, and this is not it.
            raise_interrupt=False,
        )
        try:
            for batch in changes:
                if stop.is_set():
                    return False
                self._on_change(root_id)
                if _added_directory(batch):
                    return True
            return False
        finally:
            # Explicitly, before the caller places the next set: abandoning the
            # generator would release these watches only when it happened to be
            # collected, and until then both lists count against the inotify
            # budget the module docstring warns a large archive can exhaust.
            changes.close()
