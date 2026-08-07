"""Pipeline pause/resume: the whole thing (#19) and one stage at a time (#32).

Only one archive is ever open at a time (see JobManager.current_root_id), so
pause is a single global flag rather than per-archive state. Pausing must
reuse the existing cancellation mechanism -- the same threading.Event checked
at batch checkpoints that close_archive/stop_archive already use -- so work
resumes from the last committed batch instead of being killed outright.

Per-stage pause is the same mechanism narrowed to the kinds behind one display
card, and it only means anything while the global switch is off: its whole
point is letting the rest of the pipeline keep running.
"""

from __future__ import annotations

import threading

from trove.config import Config
from trove.pipeline import manager as jobs_mod
from trove.pipeline import stages as stages_mod
from trove.pipeline import status as status_mod
from trove.services import archives as archives_mod
from trove.services import models as models_mod


def _job_manager(tmp_path, monkeypatch, cfg=None):
    # Everything stays under tmp_path: archive_db_path/archive_cache_dir
    # normally resolve under the user's real ~/.local/share/trove,
    # which must never be touched by a test.
    monkeypatch.setattr(Config, "archive_db_path", lambda self, aid: str(tmp_path / "archive.db"))
    monkeypatch.setattr(Config, "archive_cache_dir", lambda self, aid: str(tmp_path / "cache"))
    # The archives here are fakes that were never configured, so they run every
    # feature -- including the three with weights to download, which a tick
    # would otherwise start fetching before any stage. That job is
    # test_model_fetch.py's subject; here it would only add a "models" entry to
    # every assertion about what a pause did or did not start.
    monkeypatch.setattr(models_mod, "missing", lambda cfg, enabled: ())
    jm = jobs_mod.JobManager(cfg if cfg is not None else Config())
    # These tests drive scheduler.tick() by hand. Park the scheduler thread so
    # it cannot also fire on its own timer after the test has torn its stubs down.
    jm.scheduler.stop()
    return jm


_QUEUED_SCAN_STAGE = {
    "kind": "scan",
    "card": "scan",
    "counted": True,
    "state": "queued",
    "pending": 3,
    "progress": None,
    "blocker": None,
    "error": None,
}


def _rig_tick(jm, monkeypatch, started):
    """Wire scheduler.tick() to see one open archive with one queued (startable)
    stage, and capture what it tries to start instead of really starting it."""
    jm._open_root_id = 1
    monkeypatch.setattr(
        archives_mod, "archives", lambda cfg: [{"id": 1, "path": "/fake", "exists": True}]
    )
    monkeypatch.setattr(
        stages_mod,
        "stage_states",
        lambda cfg, jobs, root_id, root_path, allow_walk=False: [dict(_QUEUED_SCAN_STAGE)],
    )
    monkeypatch.setattr(
        jm,
        "start",
        lambda kind, root_id=None, root_path=None, force=False, files_on_disk=None: (
            started.append(kind) or {"id": 1}
        ),
    )


# ---------------------------------------------------------------------------
# set_paused() gates scheduler.tick()
# ---------------------------------------------------------------------------


def test_auto_tick_starts_queued_work_when_not_paused(tmp_path, monkeypatch):
    jm = _job_manager(tmp_path, monkeypatch)
    started = []
    _rig_tick(jm, monkeypatch, started)

    assert jm.paused() is False
    assert jm.scheduler.tick() is True
    assert started == ["scan"]


def test_set_paused_true_makes_auto_tick_return_false_and_start_nothing(tmp_path, monkeypatch):
    jm = _job_manager(tmp_path, monkeypatch)
    started = []
    _rig_tick(jm, monkeypatch, started)

    jm.set_paused(True)
    assert jm.paused() is True
    assert jm.scheduler.tick() is False
    assert started == []


def test_set_paused_false_restores_normal_auto_tick_behaviour(tmp_path, monkeypatch):
    jm = _job_manager(tmp_path, monkeypatch)
    started = []
    _rig_tick(jm, monkeypatch, started)

    jm.set_paused(True)
    assert jm.scheduler.tick() is False

    jm.set_paused(False)
    assert jm.paused() is False
    assert jm.scheduler.tick() is True
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
    jm.scheduler.interval = jm.scheduler.AUTO_MAX

    jm.set_paused(False)

    assert jm.scheduler.interval == jm.scheduler.AUTO_MIN


# ---------------------------------------------------------------------------
# Pause belongs to ONE archive, and round-trips through Config persistence
# ---------------------------------------------------------------------------


def _registered(cfg, *paths) -> list[int]:
    """Register archives without touching disk beyond config.json."""
    ids = []
    for path in paths:
        aid = max((a["id"] for a in cfg.archives), default=0) + 1
        cfg.archives.append({"id": aid, "path": path, "added_at": "now"})
        ids.append(aid)
    cfg.save()
    return ids


def _open(jm, root_id: int) -> None:
    """jm.open_archive() without its database work.

    open_archive's first act is _open_db (schema migration, root
    reconciliation, an embedder check) against a real archive database. None of
    that is what these tests are about, and building one per test would make
    them minutes long -- what matters here is which archive is open and what
    its pause state is.
    """
    jm._open_db = lambda rid: _NullConn()  # type: ignore[method-assign]
    jm.open_archive(root_id)


class _NullConn:
    def close(self) -> None:
        pass


def test_pipeline_paused_defaults_false_and_round_trips_through_config(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    cfg = Config.load()
    assert cfg.pipeline_paused is False
    aid = _registered(cfg, "/fake/a")[0]
    assert cfg.archive_pause(aid) == (False, [])

    cfg.set_archive_pause(aid, True, ["detect"])

    assert Config.load().archive_pause(aid) == (True, ["detect"])


def test_set_paused_persists_per_archive_and_seeds_a_fresh_job_manager(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    cfg = Config.load()
    aid = _registered(cfg, "/fake/a")[0]
    jm = _job_manager(tmp_path, monkeypatch, cfg)
    _open(jm, aid)
    assert jm.paused() is False

    jm.set_paused(True)
    assert jm.paused() is True

    # A new process would reload Config from disk and reopen the archive; the
    # state open_archive loads must be what set_paused() wrote for THAT archive.
    reloaded_cfg = Config.load()
    assert reloaded_cfg.archive_pause(aid) == (True, [])
    jm2 = _job_manager(tmp_path, monkeypatch, reloaded_cfg)
    _open(jm2, aid)
    assert jm2.paused() is True


def test_a_pause_does_not_follow_the_user_to_another_archive(monkeypatch, tmp_path):
    """The bug this is here for: pausing archive A left archive B paused too,
    with no running work and a Resume button the user never pressed."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    cfg = Config.load()
    a, b = _registered(cfg, "/fake/a", "/fake/b")
    jm = _job_manager(tmp_path, monkeypatch, cfg)

    _open(jm, a)
    jm.set_paused(True)
    jm.set_stage_paused("detect", True)

    _open(jm, b)
    assert jm.paused() is False
    assert jm.paused_stages() == set()

    # ...and A still remembers its own when the user goes back.
    _open(jm, a)
    assert jm.paused() is True
    assert jm.paused_stages() == {"detect"}


def test_resuming_an_archive_stops_it_inheriting_the_app_wide_default(monkeypatch, tmp_path):
    """cfg.pipeline_paused is only a default for an archive with no state of
    its own, so pressing Resume has to record False rather than clear the key
    and fall back through it again on the next open."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    cfg = Config.load()
    cfg.pipeline_paused = True
    aid = _registered(cfg, "/fake/a")[0]
    jm = _job_manager(tmp_path, monkeypatch, cfg)

    _open(jm, aid)
    assert jm.paused() is True  # the default applies to an archive with no state

    jm.set_paused(False)
    _open(jm, aid)

    assert jm.paused() is False


# ---------------------------------------------------------------------------
# status.snapshot() surfaces the flag and adjusts cards/overall
# ---------------------------------------------------------------------------


class _FakeJobs:
    """Minimal stand-in for JobManager, matching the constraint that
    status.snapshot() must be defensive when `paused` is absent (older
    fakes in other tests construct JobManager-shaped objects without it)."""

    def __init__(self, paused=False, stages=()):
        self._paused = paused
        self._stages = set(stages)

    def paused(self):
        return self._paused

    def paused_stages(self):
        return set(self._stages)

    def list(self, root_id=None):
        return []

    def disk_count(self, root_id, root_path, max_age=None, allow_walk=True):
        return 0

    def dedup_needed(self, root_id):
        return False


def test_snapshot_reports_unpaused_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "archive_db_path", lambda self, aid: str(tmp_path / "archive.db"))
    from trove.db import database as db

    conn = db.connect(tmp_path / "archive.db")
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/x','now')")
    # An archive is only idle once a completed scan accounts for what is on
    # disk (db.scan_settled); without one there is always a scan owed.
    run = db.scan_run_start(conn, 1, ["/x"])
    conn.execute("UPDATE scan_runs SET finished_at='now', files_on_disk=0 WHERE id=?", (run,))
    conn.commit()
    conn.close()

    snap = status_mod.snapshot(Config(), _FakeJobs(paused=False), 1, str(tmp_path))
    assert snap["paused"] is False
    assert snap["overall"] == "idle"


def test_snapshot_marks_queued_cards_paused_without_changing_state(tmp_path, monkeypatch):
    monkeypatch.setattr(
        stages_mod,
        "stage_states",
        lambda cfg, jobs, root_id, root_path, allow_walk=False: [dict(_QUEUED_SCAN_STAGE)],
    )

    snap = status_mod.snapshot(Config(), _FakeJobs(paused=True), 1, str(tmp_path))

    assert snap["paused"] is True
    assert snap["overall"] == "paused"
    scan_card = next(c for c in snap["stages"] if c["id"] == "scan")
    assert scan_card["state"] == "queued"  # untouched -- client/scheduler key off this
    assert scan_card["message"] == "Paused"


def test_snapshot_defensive_when_jobs_has_no_paused_method(tmp_path, monkeypatch):
    """Other tests construct JobManager-shaped fakes that predate this
    feature; snapshot() must not blow up against one missing .paused()."""
    monkeypatch.setattr(Config, "archive_db_path", lambda self, aid: str(tmp_path / "archive.db"))
    from trove.db import database as db

    conn = db.connect(tmp_path / "archive.db")
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/x','now')")
    # An archive is only idle once a completed scan accounts for what is on
    # disk (db.scan_settled); without one there is always a scan owed.
    run = db.scan_run_start(conn, 1, ["/x"])
    conn.execute("UPDATE scan_runs SET finished_at='now', files_on_disk=0 WHERE id=?", (run,))
    conn.commit()
    conn.close()

    class NoPausedAttr:
        def list(self, root_id=None):
            return []

        def disk_count(self, root_id, root_path, max_age=None, allow_walk=True):
            return 0

        def dedup_needed(self, root_id):
            return False

    snap = status_mod.snapshot(Config(), NoPausedAttr(), 1, str(tmp_path))
    assert snap["paused"] is False
    assert snap["paused_stages"] == []


# ---------------------------------------------------------------------------
# Per-stage pause (#32): one card off, the rest of the pipeline untouched
# ---------------------------------------------------------------------------

# scan ∥ semantic: two stages that can run at the same time, on different cards,
# so pausing one must demonstrably leave the other startable.
_QUEUED_SEMANTIC_STAGE = {
    "kind": "semantic",
    "card": "semantic",
    "counted": True,
    "state": "queued",
    "pending": 7,
    "progress": None,
    "blocker": None,
    "error": None,
}


def _rig_two_stages(jm, monkeypatch, started):
    jm._open_root_id = 1
    monkeypatch.setattr(
        archives_mod, "archives", lambda cfg: [{"id": 1, "path": "/fake", "exists": True}]
    )
    monkeypatch.setattr(
        stages_mod,
        "stage_states",
        lambda cfg, jobs, root_id, root_path, allow_walk=False: [
            dict(_QUEUED_SCAN_STAGE),
            dict(_QUEUED_SEMANTIC_STAGE),
        ],
    )
    monkeypatch.setattr(
        jm,
        "start",
        lambda kind, root_id=None, root_path=None, force=False, files_on_disk=None: (
            started.append(kind) or {"id": 1}
        ),
    )


def test_pausing_one_stage_leaves_the_others_running(tmp_path, monkeypatch):
    jm = _job_manager(tmp_path, monkeypatch)
    started = []
    _rig_two_stages(jm, monkeypatch, started)

    jm.set_stage_paused("scan", True)

    assert jm.stage_paused("scan") is True
    assert jm.scheduler.tick() is True
    assert started == ["semantic"]  # scan skipped, its sibling still starts


def test_resuming_a_stage_starts_it_again(tmp_path, monkeypatch):
    jm = _job_manager(tmp_path, monkeypatch)
    started = []
    _rig_two_stages(jm, monkeypatch, started)

    jm.set_stage_paused("scan", True)
    jm.scheduler.tick()
    started.clear()
    jm.set_stage_paused("scan", False)

    assert jm.paused_stages() == set()
    assert jm.scheduler.tick() is True
    assert started == ["scan", "semantic"]


def test_pausing_the_scan_card_stops_enrich_too(tmp_path, monkeypatch):
    """One card, two kinds: the Scan card fuses scan ∥ enrich, so its button has
    to stop both -- a paused Scan that keeps chewing through metadata would be
    the same broken promise the per-job controls were added to fix."""
    jm = _job_manager(tmp_path, monkeypatch)
    started = []
    jm._open_root_id = 1
    monkeypatch.setattr(
        archives_mod, "archives", lambda cfg: [{"id": 1, "path": "/fake", "exists": True}]
    )
    enrich = dict(_QUEUED_SCAN_STAGE, kind="enrich")
    monkeypatch.setattr(
        stages_mod,
        "stage_states",
        lambda cfg, jobs, root_id, root_path, allow_walk=False: [dict(_QUEUED_SCAN_STAGE), enrich],
    )
    monkeypatch.setattr(
        jm,
        "start",
        lambda kind, root_id=None, root_path=None, force=False, files_on_disk=None: (
            started.append(kind) or {"id": 1}
        ),
    )

    jm.set_stage_paused("scan", True)

    assert jm.scheduler.tick() is False
    assert started == []


def test_pausing_a_stage_cancels_only_that_stage_s_running_jobs(tmp_path, monkeypatch):
    jm = _job_manager(tmp_path, monkeypatch)
    with jm._lock:
        for jid, kind in ((1, "enrich"), (2, "semantic")):
            jm._jobs[jid] = jobs_mod.Job(id=jid, kind=kind, root_id=1, root_path="/fake")
            jm._cancels[jid] = threading.Event()

    jm.set_stage_paused("scan", True)  # the scan card owns scan + enrich

    assert jm._cancels[1].is_set()
    assert not jm._cancels[2].is_set()


def test_paused_stage_does_not_keep_the_scheduler_at_its_fast_interval(tmp_path, monkeypatch):
    """A queued-but-paused stage is not outstanding work: reporting it as such
    would pin the idle backoff (and its ~150k-file disk walk) to AUTO_MIN."""
    jm = _job_manager(tmp_path, monkeypatch)
    started = []
    _rig_tick(jm, monkeypatch, started)

    jm.set_stage_paused("scan", True)

    assert jm.scheduler.tick() is False
    assert started == []


def test_stages_blocked_behind_a_paused_stage_are_not_outstanding(tmp_path, monkeypatch):
    jm = _job_manager(tmp_path, monkeypatch)
    jm._open_root_id = 1
    monkeypatch.setattr(
        archives_mod, "archives", lambda cfg: [{"id": 1, "path": "/fake", "exists": True}]
    )
    dedup = {
        "kind": "dedup",
        "card": "dedup",
        "counted": False,
        "state": "queued",
        "pending": 1,
        "progress": None,
        "blocker": None,
        "error": None,
    }
    places = {
        "kind": "places",
        "card": "places",
        "counted": True,
        "state": "blocked",
        "pending": 4,
        "progress": None,
        "blocker": "dedup",
        "error": None,
    }
    monkeypatch.setattr(
        stages_mod,
        "stage_states",
        lambda cfg, jobs, root_id, root_path, allow_walk=False: [dedup, places],
    )

    jm.set_stage_paused("dedup", True)

    assert jm.scheduler.tick() is False


def test_paused_stages_persist_and_seed_a_fresh_job_manager(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    cfg = Config.load()
    assert cfg.paused_stages == []
    aid = _registered(cfg, "/fake/a")[0]
    jm = _job_manager(tmp_path, monkeypatch, cfg)
    _open(jm, aid)

    jm.set_stage_paused("detect", True)

    assert Config.load().archive_pause(aid) == (False, ["detect"])
    jm2 = _job_manager(tmp_path, monkeypatch, Config.load())
    _open(jm2, aid)
    assert jm2.stage_paused("detect") is True


def test_snapshot_marks_the_paused_card_and_what_waits_behind_it(tmp_path, monkeypatch):
    dedup = {
        "kind": "dedup",
        "card": "dedup",
        "counted": False,
        "state": "queued",
        "pending": 1,
        "progress": None,
        "blocker": None,
        "error": None,
    }
    places = {
        "kind": "places",
        "card": "places",
        "counted": True,
        "state": "blocked",
        "pending": 4,
        "progress": None,
        "blocker": "dedup",
        "error": None,
    }
    monkeypatch.setattr(
        stages_mod,
        "stage_states",
        lambda cfg, jobs, root_id, root_path, allow_walk=False: [dedup, places],
    )

    snap = status_mod.snapshot(
        Config(), _FakeJobs(paused=False, stages={"dedup"}), 1, str(tmp_path)
    )

    assert snap["paused"] is False  # the pipeline as a whole is not paused
    assert snap["paused_stages"] == ["dedup"]
    cards = {c["id"]: c for c in snap["stages"]}
    assert cards["dedup"]["paused"] is True and cards["dedup"]["message"] == "Paused"
    # Places is not itself paused, but it can never start while dedup is.
    assert cards["places"]["paused"] is False
    assert cards["places"]["stalled"] is True
    assert "paused" in cards["places"]["message"]
    # Nothing can move, so the pipeline is stopped rather than working.
    assert snap["overall"] == "paused"


def test_snapshot_still_reports_working_when_another_stage_can_run(tmp_path, monkeypatch):
    monkeypatch.setattr(
        stages_mod,
        "stage_states",
        lambda cfg, jobs, root_id, root_path, allow_walk=False: [
            dict(_QUEUED_SCAN_STAGE),
            dict(_QUEUED_SEMANTIC_STAGE),
        ],
    )

    snap = status_mod.snapshot(Config(), _FakeJobs(paused=False, stages={"scan"}), 1, str(tmp_path))

    assert snap["overall"] == "working"
    cards = {c["id"]: c for c in snap["stages"]}
    assert cards["scan"]["paused"] is True
    assert cards["semantic"]["paused"] is False and cards["semantic"]["stalled"] is False
