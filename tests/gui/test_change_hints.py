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
