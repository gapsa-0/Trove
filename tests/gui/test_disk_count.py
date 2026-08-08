"""Opening a large archive must not start one tree walk per caller.

The disk count is the expensive half of pipeline freshness, and the cache that
normally hides it is empty for the archive the user just opened. Three callers
reach that cold root at once -- the polled status endpoint, the scheduler tick,
and every further poll that fires while the first is still counting -- and each
one used to start its own walk of the same tree. On a 97k-file archive that is
~20s each, contending for one disk, while the browser's connection budget fills
with status requests that have not come back.
"""

from __future__ import annotations

import threading
import time

from trove.pipeline import archives


def test_concurrent_cold_callers_share_one_walk(tmp_path, monkeypatch):
    disk = archives.DiskCounts(lambda: False)
    started = []
    barrier = threading.Event()

    def fake_count_files(path):
        started.append(time.monotonic())
        barrier.wait(2.0)
        return 97078

    monkeypatch.setattr("trove.scan.walker.count_files", fake_count_files)

    results: list[int | None] = []
    threads = [
        threading.Thread(target=lambda: results.append(disk.count(1, str(tmp_path))))
        for _ in range(5)
    ]
    for t in threads:
        t.start()
        time.sleep(0.02)  # the poll interval, compressed: each arrives mid-walk
    barrier.set()
    for t in threads:
        t.join(5.0)

    assert len(started) == 1, "each caller walked the tree instead of sharing one walk"
    assert results == [97078] * 5, "the waiters did not get the walk's answer"


def test_a_warm_count_never_queues_behind_a_walk(tmp_path, monkeypatch):
    """The lock is for cold roots only. A caller with a fresh count must not
    wait on another root's walk, nor on a background refresh of its own."""
    disk = archives.DiskCounts(lambda: False)
    disk._cache[1] = (time.monotonic(), 11)
    holding = threading.Event()

    def fake_count_files(path):
        holding.set()
        time.sleep(1.0)
        return 22

    monkeypatch.setattr("trove.scan.walker.count_files", fake_count_files)

    walker = threading.Thread(target=lambda: disk.count(2, str(tmp_path)))
    walker.start()
    assert holding.wait(2.0)
    t = time.monotonic()
    assert disk.count(1, str(tmp_path)) == 11
    assert time.monotonic() - t < 0.5, "a cached count waited on someone else's walk"
    walker.join(5.0)


def test_a_missing_folder_still_answers_none(tmp_path, monkeypatch):
    """The not-a-directory branch moved inside the lock; it must still report
    None and forget any count it had, rather than walking."""
    disk = archives.DiskCounts(lambda: False)
    disk._cache[1] = (0.0, 11)  # stale, so the walk path is taken
    monkeypatch.setattr(
        "trove.scan.walker.count_files",
        lambda path: (_ for _ in ()).throw(AssertionError("walked a folder that is gone")),
    )
    assert disk.count(1, str(tmp_path / "gone")) is None
    assert 1 not in disk._cache
