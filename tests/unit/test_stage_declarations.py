"""Two facts about a stage that are stated twice, in two files, and must agree.

Whether a stage takes the single DB-writer lock is written down in
``pipeline/stages.py`` (``LOCK_KINDS`` / ``PARALLEL_KINDS``, which the scheduler
reads to decide what may start) and again in ``pipeline/job.py``
(``Runner.takes_write_lock``, which the manager reads to decide what to hold
while it runs). Nothing tied the two together, so a stage could be serialised by
one and run lock-free by the other.

The second test is the subtler one. ``scheduler._may_start`` asks
``kind in PARALLEL_KINDS`` and falls through to the single-writer branch when the
answer is no -- so a kind left out of *both* sets is serialised silently, by
omission, rather than because anyone decided it should be. A new stage that
forgets its entry gets the safe behaviour and no warning, which is exactly the
kind of accident that only shows up as an unexplained slowdown much later.
"""

from __future__ import annotations

from trove.pipeline import stages
from trove.pipeline.runners import RUNNERS


def test_a_runner_locks_exactly_as_its_stage_declares():
    for kind, runner in RUNNERS.items():
        # Skip the jobs that are not pipeline stages (the re-cluster jobs a user
        # action kicks, and the model fetch): they have no StageDef to agree with.
        if not stages.is_stage_kind(kind):
            continue
        assert runner.takes_write_lock == (kind in stages.LOCK_KINDS), (
            f"{kind}: Runner.takes_write_lock={runner.takes_write_lock} but "
            f"{'in' if kind in stages.LOCK_KINDS else 'not in'} stages.LOCK_KINDS"
        )


def test_every_stage_kind_is_either_parallel_or_locking():
    for sd in stages.STAGES:
        locking = sd.kind in stages.LOCK_KINDS
        parallel = sd.kind in stages.PARALLEL_KINDS
        assert locking ^ parallel, (
            f"{sd.kind} is in {'both' if locking else 'neither'} of LOCK_KINDS and "
            "PARALLEL_KINDS; the scheduler needs it in exactly one"
        )


def test_every_stage_kind_has_a_runner_to_dispatch_to():
    """A StageDef with no runner is a stage the scheduler queues forever.

    ``manager._run`` raises ``unknown job kind`` when it cannot find one, so the
    stage errors, cools down, and is retried on a loop for the life of the
    session.
    """
    assert {sd.kind for sd in stages.STAGES} <= set(RUNNERS)
