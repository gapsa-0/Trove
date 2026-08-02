"""The seam every pipeline stage is dispatched through.

``JobManager._dispatch`` reads the two flags a ``Runner`` declares and sets the
job up accordingly: whether to hold the single-writer lock for the whole run,
and whether to open a connection at all. Getting either wrong is a concurrency
bug that a green suite would not notice -- two wholesale rewrites overlapping,
or a runner handed a connection it was supposed to open itself.

This file exists because the first runner moved into the registry was covered
by nothing: planting ``raise RuntimeError`` in its body left the whole suite
green. The runners' own behaviour is covered elsewhere (test_dedup,
test_scan_settles, test_semantic_video); what is tested here is only the
handover.
"""

from __future__ import annotations

import threading
import time

import pytest
from helpers import wait_until

from organize_archive.config import Config
from organize_archive.pipeline import manager as jobs_mod
from organize_archive.pipeline.job import JobContext, Runner


class _FakeConn:
    """Stands in for the per-archive connection _open_db hands the runner."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture
def jm(monkeypatch):
    """A JobManager whose jobs never touch a database.

    Mirrors test_job_logging's fixture, and for the same reason: these tests are
    about what happens *around* a runner, not about any runner's own work.
    """
    monkeypatch.setattr(jobs_mod.JobManager, "_open_db", lambda self, root_id: _FakeConn())
    manager = jobs_mod.JobManager(Config())
    try:
        yield manager
    finally:
        manager.shutdown()


def _register(monkeypatch, runner: Runner) -> None:
    """Put a runner in the live registry for one test.

    ``setitem`` on the real dict rather than a fake registry: ``_run`` looks the
    kind up in ``RUNNERS`` by module attribute, so patching the dict is what
    actually exercises the path the scheduler uses.
    """
    monkeypatch.setitem(jobs_mod.RUNNERS, runner.kind, runner)


def _run_to_completion(manager, kind, timeout=5.0):
    """Start a job and wait for the worker thread to be finished with it.

    Waits on ``finished_at`` rather than ``status`` because ``_run`` sets the
    terminal status first and does its bookkeeping after; polling status alone
    can return while the worker is still inside the ``finally``.
    """
    started = manager.start(kind)
    assert "error" not in started, started
    job = manager._jobs[started["id"]]
    wait_until(
        lambda: job.finished_at is not None, timeout=timeout, what=f"the {kind} job to finish"
    )
    return job


def test_a_registered_runner_receives_a_context_for_its_job(jm, monkeypatch):
    seen: list[JobContext] = []
    _register(monkeypatch, Runner(kind="probe", run=seen.append))

    job = _run_to_completion(jm, "probe")

    assert job.status == "done"
    assert len(seen) == 1
    ctx = seen[0]
    assert ctx.job is job
    assert ctx.cfg is jm.cfg
    assert isinstance(ctx.cancel, threading.Event)


def test_a_runner_that_wants_a_connection_gets_one_and_it_is_closed(jm, monkeypatch):
    """The manager owns the connection's lifetime, not the runner -- so a runner
    that raises still cannot leak one."""
    seen = []

    def run(ctx):
        seen.append(ctx.conn)
        raise RuntimeError("boom")

    _register(monkeypatch, Runner(kind="probe", run=run))

    job = _run_to_completion(jm, "probe")

    assert job.status == "error"
    assert isinstance(seen[0], _FakeConn)
    assert seen[0].closed, "the connection must be closed even when the runner raises"


def test_a_runner_that_opens_its_own_connection_is_given_none(jm, monkeypatch):
    """``needs_connection=False`` is semantic's case: it snapshots under a
    read-only connection and writes each result in its own transaction, so the
    manager must not open one for it."""
    seen = []
    _register(
        monkeypatch,
        Runner(kind="probe", run=lambda ctx: seen.append(ctx.conn), needs_connection=False),
    )

    job = _run_to_completion(jm, "probe")

    assert job.status == "done"
    assert seen == [None]


def test_the_write_lock_is_held_for_a_runner_that_declares_it(jm, monkeypatch):
    held = []
    _register(
        monkeypatch,
        Runner(kind="probe", run=lambda ctx: held.append(jm._write_lock.locked())),
    )

    _run_to_completion(jm, "probe")

    assert held == [True]


def test_the_write_lock_is_not_held_for_a_parallel_runner(jm, monkeypatch):
    """scan, enrich and semantic overlap freely; holding the writer lock for
    them would serialise the whole pipeline behind a multi-hour scan."""
    held = []
    _register(
        monkeypatch,
        Runner(
            kind="probe",
            run=lambda ctx: held.append(jm._write_lock.locked()),
            takes_write_lock=False,
        ),
    )

    _run_to_completion(jm, "probe")

    assert held == [False]


def test_a_cancelled_runner_is_recorded_as_cancelled_not_failed(jm, monkeypatch):
    """``ctx.raise_if_cancelled()`` is the checkpoint every long loop must call.
    It has to reach the manager as a cancellation, because a pause or an archive
    switch goes through this path and neither is a failure."""

    def run(ctx):
        ctx.cancel.set()
        ctx.raise_if_cancelled()
        raise AssertionError("raise_if_cancelled did not raise on a set event")

    _register(monkeypatch, Runner(kind="probe", run=run))

    job = _run_to_completion(jm, "probe")

    assert job.status == "cancelled"
    assert job.message == "cancelled; progress saved"


def test_progress_from_the_context_is_wired_to_the_jobs_cancel_event(jm, monkeypatch):
    """``ctx.progress()`` exists so a runner cannot build a progress adapter
    bound to the wrong event -- one that never raises is a job that ignores
    cancellation, which makes the app un-quittable."""

    def run(ctx):
        prog = ctx.progress()
        prog.update(3)
        assert ctx.job.done == 3
        ctx.cancel.set()
        prog.update(4)  # must raise now that the event is set

    _register(monkeypatch, Runner(kind="probe", run=run))

    job = _run_to_completion(jm, "probe")

    assert job.status == "cancelled"
    assert job.done == 3, "the update before the cancel must still have landed"


def test_an_unknown_job_kind_is_an_error_not_a_silent_no_op(jm):
    job = _run_to_completion(jm, "no-such-stage")

    assert job.status == "error"
    assert "no-such-stage" in job.message


def test_shutdown_does_not_wait_for_a_runner_loading_a_model(jm, monkeypatch):
    """The "app takes forever to close" bug, pinned.

    ``detect`` and ``semantic`` open ONNX sessions before their first
    checkpoint: seconds of native code that cannot see the cancel event. The
    fake below stands in for that call. Shutdown must not spend its timeout
    waiting on a thread that provably cannot answer -- the daemon thread is
    reaped by process exit, which is what happened after the timeout anyway.
    """
    loading, release = threading.Event(), threading.Event()

    def run(ctx):
        with ctx.uninterruptible("loading detection models"):
            loading.set()
            release.wait(timeout=5)

    _register(monkeypatch, Runner(kind="probe", run=run))
    jm.start("probe")
    wait_until(lambda: loading.is_set(), timeout=5, what="the runner to reach the model load")

    try:
        started = time.monotonic()
        assert jm.shutdown(timeout=5.0) is True
        assert time.monotonic() - started < 1.0, "shutdown waited on an un-cancellable section"
    finally:
        release.set()


def test_a_cancel_arriving_around_a_model_load_is_honoured_at_its_edges(jm, monkeypatch):
    """The section itself cannot be interrupted, but its two edges can be.

    A cancel that arrives while the model is loading has to take effect the
    moment the load returns, not at whatever checkpoint the chunk loop reaches
    next -- otherwise skipping the wait above would just move the delay.
    """
    reached = []

    def run(ctx):
        with ctx.uninterruptible("loading detection models"):
            reached.append("loading")
            ctx.cancel.set()  # the shutdown that arrives mid-load
        reached.append("kept going")

    _register(monkeypatch, Runner(kind="probe", run=run))

    job = _run_to_completion(jm, "probe")

    assert reached == ["loading"], "the runner continued past a cancel set during the load"
    assert job.status == "cancelled"
    assert job.uninterruptible is False, "the marker must be cleared on the way out"


def test_a_job_that_ignores_its_cancel_event_still_times_shutdown_out(jm, monkeypatch):
    """The complement of the two above: skipping the wait is for native code
    only. A runner looping without a checkpoint is a bug, and shutdown must go
    on reporting it rather than returning a clean True."""
    release = threading.Event()
    _register(monkeypatch, Runner(kind="probe", run=lambda ctx: release.wait(timeout=5)))
    jm.start("probe")

    try:
        assert jm.shutdown(timeout=0.2) is False
    finally:
        release.set()


def test_the_uninterruptible_marker_stays_out_of_the_polled_payload(jm, monkeypatch):
    """It is a pipeline-internal detail. The GUI polls ``public()`` about once a
    second and has no use for it."""
    _register(monkeypatch, Runner(kind="probe", run=lambda ctx: None))

    job = _run_to_completion(jm, "probe")

    assert "uninterruptible" not in job.public()
