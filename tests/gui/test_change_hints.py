"""Noticing that files arrived, without waiting out the poll's backoff.

The scheduler's idle interval backs off to five minutes on a quiet archive, and
the disk count behind it is cached for a minute more, so files dropped into a
folder could sit there unnoticed for the length of both. A hint short-circuits
that: expire the cached count, wake the scheduler, and let the tick that
follows walk and decide as it always did.

Nothing here decides anything about the files themselves. That is the property
these hold to -- a hint is allowed to be wrong, repeated or missing, because the
poll behind it is what is actually correct. It is also what lets the filesystem
watcher be optional and best-effort.
"""

from __future__ import annotations

import threading
import time

from trove.config import Config
from trove.pipeline import archives, watcher
from trove.pipeline import manager as jobs_mod
from trove.scan import walker


def _job_manager(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "archive_db_path", lambda self, aid: str(tmp_path / "archive.db"))
    monkeypatch.setattr(Config, "archive_cache_dir", lambda self, aid: str(tmp_path / "cache"))
    jm = jobs_mod.JobManager(Config())
    jm.scheduler.stop()  # no polling thread; these drive it by hand
    return jm


def test_a_hint_expires_the_cached_count_and_wakes_the_scheduler(tmp_path, monkeypatch):
    """The two things a hint does, and the only two. The cached count matters as
    much as the wake-up: a tick that reuses a count from before the files landed
    looks at the archive and sees nothing new.

    Expired, not dropped -- see DiskCounts.invalidate. The next caller re-walks
    either way; keeping the value is what lets the poll answer with the previous
    number instead of "no idea" while that walk runs."""
    jm = _job_manager(tmp_path, monkeypatch)
    jm._disk._cache[1] = (time.monotonic(), 42)
    woken = []
    monkeypatch.setattr(jm.scheduler, "nudge", lambda: woken.append(True))
    monkeypatch.setattr(jm.scheduler, "stopping", lambda: False)

    jm.note_files_changed(1)

    stamp, count = jm._disk._cache[1]
    assert count == 42, "the hint threw away a count it had no reason to doubt"
    assert time.monotonic() - stamp > archives.WALK_TTL, "the count did not expire"
    assert woken == [True]


def test_a_hint_leaves_the_poll_an_answer_to_give(tmp_path, monkeypatch):
    """What the user sees, and the reason the line above says "expires".

    Every return to the window sends a hint (status.js binds focus and
    visibilitychange), which fires at most every WALK_FLOOR seconds but is
    deferred rather than dropped, so one always lands. While it dropped the
    cached count, the polled path answered None -- which _scan_backlog reports
    as a `checking` stage and the Overview draws as "Counting files in this
    folder…" over the top of the Indexing card's own result, for the ~20s a
    97k-file walk takes. Alt-tab a few times and it was never off the screen.

    The walk is kicked either way; only the answer given while it runs changes.
    """
    jm = _job_manager(tmp_path, monkeypatch)
    jm._disk._cache[1] = (time.monotonic(), 97_078)
    monkeypatch.setattr(jm.scheduler, "nudge", lambda: None)
    monkeypatch.setattr(jm.scheduler, "stopping", lambda: False)
    walked = []
    monkeypatch.setattr("trove.scan.walker.count_files", lambda path: walked.append(path) or 97_099)
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: True)

    jm.note_files_changed(1)

    # The poll never walks on its own thread, so it answers from the cache --
    # with the last count, not with None.
    assert jm.disk_count(1, str(tmp_path), allow_walk=False) == 97_078
    # ...and the re-walk it needs was still started.
    assert jm.disk_count(1, str(tmp_path), allow_walk=True) == 97_099
    assert walked, "the expired count was never re-walked"


def test_a_second_hint_straight_away_does_not_cost_a_second_walk(tmp_path, monkeypatch):
    """Acting on a hint means walking the whole tree, and files dragged in one
    at a time would otherwise be a walk each."""
    jm = _job_manager(tmp_path, monkeypatch)
    woken = []
    monkeypatch.setattr(jm.scheduler, "nudge", lambda: woken.append(True))
    monkeypatch.setattr(jm.scheduler, "stopping", lambda: False)

    jm.note_files_changed(1)
    jm.note_files_changed(1)
    jm.note_files_changed(1)

    assert woken == [True]


def test_the_hint_held_back_is_not_lost(tmp_path, monkeypatch):
    """It is deferred to the end of the floor, not dropped. The last file of a
    slow trickle is the one that would otherwise wait for the poll."""
    jm = _job_manager(tmp_path, monkeypatch)
    woken = []
    monkeypatch.setattr(jm.scheduler, "nudge", lambda: woken.append(True))
    monkeypatch.setattr(jm.scheduler, "stopping", lambda: False)
    monkeypatch.setattr(watcher, "WALK_FLOOR", 0.05)

    jm.note_files_changed(1)
    jm.note_files_changed(1)
    deadline = time.monotonic() + 2
    while len(woken) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert woken == [True, True]


def test_two_archives_are_throttled_apart(tmp_path, monkeypatch):
    jm = _job_manager(tmp_path, monkeypatch)
    woken = []
    monkeypatch.setattr(jm.scheduler, "nudge", lambda: woken.append(True))
    monkeypatch.setattr(jm.scheduler, "stopping", lambda: False)

    jm.note_files_changed(1)
    jm.note_files_changed(2)

    assert woken == [True, True]


def test_a_hint_on_the_way_out_is_declined(tmp_path, monkeypatch):
    """Shutdown has already cancelled everything; waking the scheduler to look
    at an archive nobody is going to work on is pure noise."""
    jm = _job_manager(tmp_path, monkeypatch)
    woken = []
    monkeypatch.setattr(jm.scheduler, "nudge", lambda: woken.append(True))
    monkeypatch.setattr(jm.scheduler, "stopping", lambda: True)

    jm.note_files_changed(1)

    assert woken == []


# -- the watcher is optional -------------------------------------------------


def test_watching_without_the_optional_dependency_is_a_no_op(monkeypatch):
    """Not installed means a slower app, never a broken one: the poll is what
    answers the question and it is untouched."""
    monkeypatch.setattr(watcher, "available", lambda: False)
    hints = []
    w = watcher.ArchiveWatcher(hints.append)

    w.start(1, "/nonexistent")

    assert w._thread is None
    w.stop()


def test_the_watcher_follows_the_open_archive(tmp_path, monkeypatch):
    """One archive at a time, matching the rule that only the open archive is
    scheduled -- which is also what bounds the inotify watches held to one
    tree."""
    started = []
    monkeypatch.setattr(watcher, "available", lambda: True)
    w = watcher.ArchiveWatcher(lambda rid: None)
    monkeypatch.setattr(w, "_watch", lambda rid, path, stop: started.append((rid, path)))

    w.start(1, str(tmp_path))
    w.start(2, str(tmp_path))
    time.sleep(0.05)

    assert started == [(1, str(tmp_path)), (2, str(tmp_path))]
    assert w._root_id == 2
    w.stop()
    assert w._root_id is None


# -- placing the watch must not stop the app ---------------------------------
#
# inotify has no recursive mode, so something must enumerate the tree and
# register each directory. Asking watchfiles to do it means asking Rust to,
# which it does with the GIL held: one unbroken 11.5 s freeze warm and 24-26 s
# cold on the 97k-file archive, during which the app serves nothing at all.
# These hold the shape that avoids it -- the directory list is built in Python,
# where scandir releases the GIL, and Rust is handed an explicit flat list.


def _watching(jm, monkeypatch):
    """Record what the manager asks the watcher to watch, without watching."""
    placed: list[tuple[int, str]] = []
    monkeypatch.setattr(jm._watcher, "start", lambda rid, path: placed.append((rid, path)))
    return placed


def test_opening_an_archive_places_the_watch_straight_away(tmp_path, monkeypatch):
    """It used to be deferred until the first disk walk, on the theory that a
    warmed page cache made the setup cheap. Measurement says otherwise -- warm
    is still 11.5 s -- so the deferral bought nothing and cost every file
    dropped in before that walk landed."""
    jm = _job_manager(tmp_path, monkeypatch)
    placed = _watching(jm, monkeypatch)
    monkeypatch.setattr(Config, "archive_path", lambda self, aid: str(tmp_path))
    monkeypatch.setattr(jm.scheduler, "nudge", lambda: None)
    monkeypatch.setattr(jm, "_open_db", lambda rid: __import__("sqlite3").connect(":memory:"))

    jm.open_archive(1)

    assert placed == [(1, str(tmp_path))]


def test_counting_files_no_longer_places_the_watch(tmp_path, monkeypatch):
    """disk_count is asked how many files are on disk, several times a minute
    from two threads. Settling a watch debt from inside it made an expensive,
    once-per-archive action a side effect of a routine reading."""
    jm = _job_manager(tmp_path, monkeypatch)
    placed = _watching(jm, monkeypatch)

    for _ in range(5):
        assert jm.disk_count(1, str(tmp_path)) is not None

    assert placed == []


def test_the_directory_list_is_built_in_python(tmp_path):
    """The property the whole fix rests on: this list is produced by scandir,
    which releases the GIL, rather than by the Rust walk that does not."""
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "photo.jpg").write_bytes(b"x")

    found = set(walker.iter_dirs(tmp_path))

    assert found == {tmp_path, tmp_path / "a", tmp_path / "a" / "b"}


def test_a_folder_that_is_gone_is_not_watched(tmp_path):
    """An unplugged drive is not something to start watching, and the poll
    already reports it as not mounted."""
    w = watcher.ArchiveWatcher(lambda rid: None)

    assert w._watch_pass(1, str(tmp_path / "unplugged"), threading.Event()) is False


def test_a_new_directory_asks_for_the_watch_to_be_replaced(tmp_path):
    """A non-recursive watch covers the directories it was given and nothing
    below them, so a folder dropped into the archive has to re-place it --
    otherwise files copied in afterwards are never reported."""
    from watchfiles import Change

    (tmp_path / "added").mkdir()

    assert watcher._added_directory({(Change.added, str(tmp_path / "added"))}) is True
    # A file is the common case and must not cost a re-place.
    (tmp_path / "photo.jpg").write_bytes(b"x")
    assert watcher._added_directory({(Change.added, str(tmp_path / "photo.jpg"))}) is False
    # Nor a directory that was only modified: it is already covered.
    assert watcher._added_directory({(Change.modified, str(tmp_path / "added"))}) is False


def test_closing_the_archive_stops_the_watch(tmp_path, monkeypatch):
    jm = _job_manager(tmp_path, monkeypatch)
    stopped = []
    monkeypatch.setattr(jm._watcher, "stop", lambda: stopped.append(True))
    jm._open_root_id = 1

    jm.close_archive(1)

    assert stopped == [True]
