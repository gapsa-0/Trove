"""A background job's outcome must be readable from the log alone.

This is the behaviour the logging work exists for: before it, a job that failed
was indistinguishable from a job that was slow, both to the user and to us. The
log now shows one of three things -- no start line (the scheduler never ran it),
a start with no end (it is genuinely stuck, and where), or an error.
"""

from __future__ import annotations

import logging
import time

import pytest

from organize_archive.config import Config
from organize_archive.gui import jobs as jobs_mod


class _FakeConn:
    """Stands in for the per-archive connection _open_db hands the runner."""

    def close(self):
        pass


@pytest.fixture
def jm(monkeypatch):
    """A JobManager whose jobs never touch a database.

    _open_db is stubbed rather than pointed at a tmp file: these tests are about
    what gets logged around a runner, not about the runner's own work. The
    scheduler thread is left alone -- its first tick is _AUTO_MIN (10s) away and
    no archive is open, so it cannot act inside a test -- but it is shut down
    afterwards so it cannot outlive the fixtures it would read.
    """
    monkeypatch.setattr(jobs_mod.JobManager, "_open_db", lambda self, root_id: _FakeConn())
    manager = jobs_mod.JobManager(Config())
    try:
        yield manager
    finally:
        manager.shutdown()


def _run_to_completion(manager, kind="dedup", timeout=5.0):
    """Start a job and wait for the worker thread to finish with it.

    Waits on ``finished_at``, not on ``status``: _run assigns the terminal status
    first and logs the outcome after, so a poll on status alone can return before
    the log record this module is asserting about has been emitted. finished_at is
    set in _run's ``finally``, which is strictly after every outcome log.
    """
    started = manager.start(kind)
    assert "error" not in started, started
    job = manager._jobs[started["id"]]
    deadline = time.monotonic() + timeout
    while job.finished_at is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert job.finished_at is not None, f"job did not finish within {timeout}s"
    return job


def test_a_failing_job_logs_an_error_with_a_traceback(jm, monkeypatch, caplog):
    def boom(self, conn, job, cancel):
        raise RuntimeError("the detector exploded")

    monkeypatch.setattr(jobs_mod.JobManager, "_run_dedup", boom)

    with caplog.at_level(logging.INFO, logger="organize_archive.gui.jobs"):
        job = _run_to_completion(jm)

    assert job.status == "error"
    failures = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(failures) == 1
    assert "job failed kind=dedup" in failures[0].getMessage()
    # Without exc_info the log says "something failed" and not where, which is
    # barely better than the silence this replaced.
    assert failures[0].exc_info is not None
    assert "the detector exploded" in caplog.text


def test_a_successful_job_logs_a_start_and_a_done(jm, monkeypatch, caplog):
    monkeypatch.setattr(jobs_mod.JobManager, "_run_dedup", lambda self, conn, job, cancel: None)

    with caplog.at_level(logging.INFO, logger="organize_archive.gui.jobs"):
        job = _run_to_completion(jm)

    assert job.status == "done"
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("job start kind=dedup") for m in messages)
    assert any(m.startswith("job done kind=dedup") for m in messages)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_cancelled_job_is_not_logged_as_a_failure(jm, monkeypatch, caplog):
    """Pause, close-archive and switch-archive all cancel at a checkpoint.

    A log that ends mid-run has to be tellable apart from one where the user
    stopped the work on purpose, so cancellation is INFO, not ERROR.
    """

    def cancelled(self, conn, job, cancel):
        raise KeyboardInterrupt

    monkeypatch.setattr(jobs_mod.JobManager, "_run_dedup", cancelled)

    with caplog.at_level(logging.INFO, logger="organize_archive.gui.jobs"):
        job = _run_to_completion(jm)

    assert job.status == "cancelled"
    assert any(
        m.startswith("job cancelled kind=dedup") for m in (r.getMessage() for r in caplog.records)
    )
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_pause_and_resume_transitions_are_logged(jm, caplog):
    with caplog.at_level(logging.INFO, logger="organize_archive.gui.jobs"):
        jm.set_paused(True)
        jm.set_paused(False)
        jm.set_stage_paused("scan", True)
        jm.set_stage_paused("scan", False)

    messages = [r.getMessage() for r in caplog.records]
    assert "pipeline paused" in messages
    assert "pipeline resumed" in messages
    assert "stage scan paused" in messages
    assert "stage scan resumed" in messages


def test_a_tick_that_starts_nothing_says_why(jm, caplog):
    """A tick that starts nothing used to look identical to no tick at all."""
    with caplog.at_level(logging.DEBUG, logger="organize_archive.gui.jobs"):
        assert jm._auto_tick() is False

    assert "no archive is open" in caplog.text
