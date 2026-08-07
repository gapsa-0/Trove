"""Noticing that files arrived, without waiting out the poll's backoff.

The scheduler's idle interval backs off to five minutes on a quiet archive, and
the disk count behind it is cached for a minute more, so files dropped into a
folder could sit there unnoticed for the length of both. A hint short-circuits
that: drop the cached count, wake the scheduler, and let the tick that follows
walk and decide as it always did.

Nothing here decides anything about the files themselves. That is the property
these hold to -- a hint is allowed to be wrong, repeated or missing, because the
poll behind it is what is actually correct. It is also what lets the filesystem
watcher be optional and best-effort.
"""

from __future__ import annotations

import time

from trove.config import Config
from trove.pipeline import manager as jobs_mod
from trove.pipeline import watcher


def _job_manager(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "archive_db_path", lambda self, aid: str(tmp_path / "archive.db"))
    monkeypatch.setattr(Config, "archive_cache_dir", lambda self, aid: str(tmp_path / "cache"))
    jm = jobs_mod.JobManager(Config())
    jm.scheduler.stop()  # no polling thread; these drive it by hand
    return jm


def test_a_hint_drops_the_cached_count_and_wakes_the_scheduler(tmp_path, monkeypatch):
    """The two things a hint does, and the only two. The cached count matters as
    much as the wake-up: a tick that reuses a count from before the files landed
    looks at the archive and sees nothing new."""
    jm = _job_manager(tmp_path, monkeypatch)
    jm._disk._cache[1] = (time.monotonic(), 42)
    woken = []
    monkeypatch.setattr(jm.scheduler, "nudge", lambda: woken.append(True))
    monkeypatch.setattr(jm.scheduler, "stopping", lambda: False)

    jm.note_files_changed(1)

    assert 1 not in jm._disk._cache
    assert woken == [True]


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


# -- the watch waits for the walk --------------------------------------------
#
# Placing a recursive watch walks the tree and stats every file, inside Rust,
# holding the GIL: ~20 s of a wholly unresponsive app on a 150k-file archive on
# a spinning disk. Opening an archive must not wait for it, and neither must
# anything else the server is being asked for. See _watch_when_walked.


def _watching(jm, monkeypatch):
    """Record what the manager asks the watcher to watch, without watching."""
    placed: list[tuple[int, str]] = []
    monkeypatch.setattr(jm._watcher, "start", lambda rid, path: placed.append((rid, path)))
    return placed


def test_opening_an_archive_does_not_place_the_watch(tmp_path, monkeypatch):
    """The expensive part is not merely moved off the request thread -- it holds
    the GIL, so a background thread would stall the app just the same. It waits
    for a walk instead."""
    jm = _job_manager(tmp_path, monkeypatch)
    placed = _watching(jm, monkeypatch)
    monkeypatch.setattr(Config, "archive_path", lambda self, aid: str(tmp_path))
    monkeypatch.setattr(jm.scheduler, "nudge", lambda: None)
    monkeypatch.setattr(jm, "_open_db", lambda rid: __import__("sqlite3").connect(":memory:"))

    jm.open_archive(1)

    assert placed == []
    assert jm._watch_owed == (1, str(tmp_path))


def test_the_walk_places_the_watch(tmp_path, monkeypatch):
    """Once a count comes back the tree's metadata is cached, which is the whole
    point of waiting: the same setup costs ~0.3 s instead of ~20 s."""
    jm = _job_manager(tmp_path, monkeypatch)
    placed = _watching(jm, monkeypatch)
    jm._watch_when_walked(1, str(tmp_path))

    assert jm.disk_count(1, str(tmp_path)) is not None
    assert placed == [(1, str(tmp_path))]


def test_the_watch_is_placed_once_however_many_walks_report_in(tmp_path, monkeypatch):
    """The scheduler's tick and the status endpoint's snapshot both call
    disk_count and routinely overlap; the debt is claimed under a lock."""
    jm = _job_manager(tmp_path, monkeypatch)
    placed = _watching(jm, monkeypatch)
    jm._watch_when_walked(1, str(tmp_path))

    for _ in range(5):
        jm.disk_count(1, str(tmp_path))

    assert placed == [(1, str(tmp_path))]


def test_a_missing_folder_places_no_watch(tmp_path, monkeypatch):
    """disk_count answers None for a folder that is gone -- an unplugged drive
    is not something to start watching, and the debt stays owed for when it
    comes back."""
    jm = _job_manager(tmp_path, monkeypatch)
    placed = _watching(jm, monkeypatch)
    gone = str(tmp_path / "unplugged")
    jm._watch_when_walked(1, gone)

    assert jm.disk_count(1, gone) is None
    assert placed == []
    assert jm._watch_owed == (1, gone)


def test_closing_the_archive_cancels_a_watch_not_yet_placed(tmp_path, monkeypatch):
    """A walk already in flight can finish after the close. Without cancelling
    the debt it would start watching an archive the user has just left."""
    jm = _job_manager(tmp_path, monkeypatch)
    placed = _watching(jm, monkeypatch)
    jm._open_root_id = 1
    jm._watch_when_walked(1, str(tmp_path))

    jm.close_archive(1)
    jm.disk_count(1, str(tmp_path))

    assert placed == []
    assert jm._watch_owed is None
