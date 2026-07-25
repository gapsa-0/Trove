"""Whole-pipeline pause/resume (things_to_fix #19).

Only one archive is ever open at a time (see JobManager.current_root_id), so
pause is a single global flag rather than per-archive state. Pausing must
reuse the existing cancellation mechanism -- the same threading.Event checked
at batch checkpoints that close_archive/stop_archive already use -- so work
resumes from the last committed batch instead of being killed outright.
"""

from __future__ import annotations

import threading

from organize_archive.config import Config
from organize_archive.gui import jobs as jobs_mod
from organize_archive.gui import pipeline as pipeline_mod
from organize_archive.gui import queries as queries_mod


def _job_manager(tmp_path, monkeypatch):
    # Everything stays under tmp_path: archive_db_path/archive_cache_dir
    # normally resolve under the user's real ~/.local/share/organize_archive,
    # which must never be touched by a test.
    monkeypatch.setattr(Config, "archive_db_path", lambda self, aid: str(tmp_path / "archive.db"))
    monkeypatch.setattr(Config, "archive_cache_dir", lambda self, aid: str(tmp_path / "cache"))
    return jobs_mod.JobManager(Config())


_QUEUED_SCAN_STAGE = {
    "kind": "scan", "card": "scan", "counted": True, "state": "queued",
    "pending": 3, "progress": None, "blocker": None, "error": None,
}


def _rig_auto_tick(jm, monkeypatch, started):
    """Wire _auto_tick() to see one open archive with one queued (startable)
    stage, and capture what it tries to start instead of really starting it."""
    jm._open_root_id = 1
    monkeypatch.setattr(queries_mod, "archives",
                        lambda cfg: [{"id": 1, "path": "/fake", "exists": True}])
    monkeypatch.setattr(pipeline_mod, "stage_states",
                        lambda cfg, jobs, root_id, root_path: [dict(_QUEUED_SCAN_STAGE)])
    monkeypatch.setattr(jm, "start", lambda kind, root_id=None, root_path=None, force=False:
                        started.append(kind) or {"id": 1})


# ---------------------------------------------------------------------------
# set_paused() gates _auto_tick()
# ---------------------------------------------------------------------------

def test_auto_tick_starts_queued_work_when_not_paused(tmp_path, monkeypatch):
    jm = _job_manager(tmp_path, monkeypatch)
    started = []
    _rig_auto_tick(jm, monkeypatch, started)

    assert jm.paused() is False
    assert jm._auto_tick() is True
    assert started == ["scan"]


def test_set_paused_true_makes_auto_tick_return_false_and_start_nothing(tmp_path, monkeypatch):
    jm = _job_manager(tmp_path, monkeypatch)
    started = []
    _rig_auto_tick(jm, monkeypatch, started)

    jm.set_paused(True)
    assert jm.paused() is True
    assert jm._auto_tick() is False
    assert started == []


def test_set_paused_false_restores_normal_auto_tick_behaviour(tmp_path, monkeypatch):
    jm = _job_manager(tmp_path, monkeypatch)
    started = []
    _rig_auto_tick(jm, monkeypatch, started)

    jm.set_paused(True)
    assert jm._auto_tick() is False

    jm.set_paused(False)
    assert jm.paused() is False
    assert jm._auto_tick() is True
    assert started == ["scan"]


# ---------------------------------------------------------------------------
# Pausing stops the CPU load: it cancels running jobs, not just future ones
# ---------------------------------------------------------------------------

def test_set_paused_true_cancels_currently_running_jobs(tmp_path, monkeypatch):
    jm = _job_manager(tmp_path, monkeypatch)
    with jm._lock:
        job = jobs_mod.Job(id=1, kind="scan", root_id=1, root_path="/fake")
        assert job.status == "running"
        jm._jobs[1] = job
        cancel = threading.Event()
        jm._cancels[1] = cancel

    jm.set_paused(True)

    assert cancel.is_set()


def test_set_paused_true_leaves_finished_jobs_alone(tmp_path, monkeypatch):
    jm = _job_manager(tmp_path, monkeypatch)
    with jm._lock:
        job = jobs_mod.Job(id=1, kind="scan", root_id=1, root_path="/fake", status="done")
        jm._jobs[1] = job
        cancel = threading.Event()
        jm._cancels[1] = cancel

    jm.set_paused(True)

    assert not cancel.is_set()


def test_set_paused_false_nudges_the_scheduler(tmp_path, monkeypatch):
    jm = _job_manager(tmp_path, monkeypatch)
    jm._auto_interval = jm._AUTO_MAX

    jm.set_paused(False)

    assert jm._auto_interval == jm._AUTO_MIN


# ---------------------------------------------------------------------------
# The flag round-trips through Config persistence
# ---------------------------------------------------------------------------

def test_pipeline_paused_defaults_false_and_round_trips_through_config(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    cfg = Config.load()
    assert cfg.pipeline_paused is False

    cfg.pipeline_paused = True
    cfg.save()

    reloaded = Config.load()
    assert reloaded.pipeline_paused is True


def test_set_paused_persists_and_seeds_a_fresh_job_manager(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    cfg = Config.load()
    jm = jobs_mod.JobManager(cfg)
    assert jm.paused() is False

    jm.set_paused(True)
    assert jm.paused() is True

    # A new process would reload Config from disk and build a new JobManager
    # from it -- the seed in __init__ must pick up what set_paused() wrote.
    reloaded_cfg = Config.load()
    assert reloaded_cfg.pipeline_paused is True
    jm2 = jobs_mod.JobManager(reloaded_cfg)
    assert jm2.paused() is True


# ---------------------------------------------------------------------------
# pipeline.snapshot() surfaces the flag and adjusts cards/overall
# ---------------------------------------------------------------------------

class _FakeJobs:
    """Minimal stand-in for JobManager, matching the constraint that
    pipeline.snapshot() must be defensive when `paused` is absent (older
    fakes in other tests construct JobManager-shaped objects without it)."""

    def __init__(self, paused=False):
        self._paused = paused

    def paused(self):
        return self._paused

    def list(self, root_id=None):
        return []

    def disk_count(self, root_id, root_path, max_age=None):
        return 0

    def dedup_needed(self, root_id):
        return False


def test_snapshot_reports_unpaused_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "archive_db_path", lambda self, aid: str(tmp_path / "archive.db"))
    from organize_archive.db import database as db
    conn = db.connect(tmp_path / "archive.db")
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/x','now')")
    conn.commit()
    conn.close()

    snap = pipeline_mod.snapshot(Config(), _FakeJobs(paused=False), 1, str(tmp_path))
    assert snap["paused"] is False
    assert snap["overall"] == "idle"


def test_snapshot_marks_queued_cards_paused_without_changing_state(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline_mod, "stage_states", lambda cfg, jobs, root_id, root_path: [
        dict(_QUEUED_SCAN_STAGE)])

    snap = pipeline_mod.snapshot(Config(), _FakeJobs(paused=True), 1, str(tmp_path))

    assert snap["paused"] is True
    assert snap["overall"] == "paused"
    scan_card = next(c for c in snap["stages"] if c["id"] == "scan")
    assert scan_card["state"] == "queued"       # untouched -- client/scheduler key off this
    assert scan_card["message"] == "Paused"


def test_snapshot_defensive_when_jobs_has_no_paused_method(tmp_path, monkeypatch):
    """Other tests construct JobManager-shaped fakes that predate this
    feature; snapshot() must not blow up against one missing .paused()."""
    monkeypatch.setattr(Config, "archive_db_path", lambda self, aid: str(tmp_path / "archive.db"))
    from organize_archive.db import database as db
    conn = db.connect(tmp_path / "archive.db")
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/x','now')")
    conn.commit()
    conn.close()

    class NoPausedAttr:
        def list(self, root_id=None):
            return []

        def disk_count(self, root_id, root_path, max_age=None):
            return 0

        def dedup_needed(self, root_id):
            return False

    snap = pipeline_mod.snapshot(Config(), NoPausedAttr(), 1, str(tmp_path))
    assert snap["paused"] is False
