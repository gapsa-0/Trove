"""Model weights arrive when the archive is created, not hours later.

The setup screen quotes a download — 275 MB for People, 689 MB for Search by
description — and then the archive was created and the download did not start.
It waited for the stage that needed the weights, and that stage waits for scan,
enrich and dedup: on a real collection, somebody chose the feature at breakfast
and the fetch began at lunchtime, invisibly, long after they had stopped
watching the screen that promised it.

So the scheduler starts a fetch job for whatever the archive's features still
owe, the moment the archive is open. What these tests hold:

* it starts for missing weights and stays out of the way otherwise — including
  the archive whose features are already downloaded, which is every archive
  after the first;
* it is a shortcut, not a gate. The stages still fetch their own weights, so
  the only thing the fetch may stop is a stage that would download the same
  files a second time while it is running;
* what it is doing is visible while it runs, since a silent hour of network is
  indistinguishable from a hung app.

Nothing here touches the network: the fetch table is stubbed at
``services/models``, which is the one place the real downloads live behind.
"""

from __future__ import annotations

import logging
import threading

import pytest
from helpers import wait_until

from trove.config import Config
from trove.errors import ModelUnavailableError
from trove.pipeline import manager as jobs_mod
from trove.pipeline import status as status_mod
from trove.pipeline.job import Job, JobContext
from trove.pipeline.runners import models as models_runner
from trove.services import archives as archives_mod
from trove.services import models as models_mod

_ROOT = 1


@pytest.fixture
def jobs(tmp_path, monkeypatch):
    """A JobManager with one open archive and its scheduler under manual control."""
    monkeypatch.setattr(Config, "archive_db_path", lambda self, aid: str(tmp_path / "archive.db"))
    monkeypatch.setattr(Config, "archive_cache_dir", lambda self, aid: str(tmp_path / "cache"))
    jm = jobs_mod.JobManager(Config(cache_dir=str(tmp_path / "cache")))
    jm.scheduler.stop()
    jm._open_root_id = _ROOT
    monkeypatch.setattr(
        archives_mod, "archives", lambda cfg: [{"id": _ROOT, "path": "/fake", "exists": True}]
    )
    return jm


def _rig(jm, monkeypatch, started, missing=("semantic",), states=()):
    """Wire tick() to see ``missing`` weights and ``states``, capturing starts."""
    from trove.pipeline import stages as stages_mod

    monkeypatch.setattr(models_mod, "missing", lambda cfg, enabled: tuple(missing))
    monkeypatch.setattr(
        stages_mod,
        "stage_states",
        lambda cfg, jobs, root_id, root_path, allow_walk=False: [dict(s) for s in states],
    )
    monkeypatch.setattr(
        jm,
        "start",
        lambda kind, root_id=None, root_path=None, force=False: started.append(kind) or {"id": 1},
    )


def _stage(kind, state="queued"):
    return {
        "kind": kind,
        "card": kind,
        "counted": True,
        "state": state,
        "pending": 12,
        "progress": None,
        "blocker": None,
        "error": None,
    }


def _running(jm, kind):
    """Register a running job of this kind, as the manager's worker would."""
    with jm._lock:
        jm._jobs[99] = Job(id=99, kind=kind, root_id=_ROOT, root_path=None)
        jm._cancels[99] = threading.Event()


# ---------------------------------------------------------------------------
# What an archive still owes
# ---------------------------------------------------------------------------


def _weights(monkeypatch, **states):
    """Stand in for the real backends: ``id=(available, ready)``."""
    monkeypatch.setattr(
        models_mod,
        "_table",
        lambda cfg: {
            fid: models_mod.Weights(lambda a=avail: a, lambda r=ready: r, lambda log, f=fid: None)
            for fid, (avail, ready) in states.items()
        },
    )


def test_only_the_features_this_archive_asked_for_are_owed(tmp_path, monkeypatch):
    """The promise the setup screen makes: a feature left off never downloads
    its models. Choosing is what stops the fetch, not luck about timing."""
    cfg = Config(cache_dir=str(tmp_path))
    _weights(monkeypatch, people=(True, False), pets=(True, False), semantic=(True, False))

    assert models_mod.missing(cfg, ["index", "duplicates", "semantic"]) == ("semantic",)
    # Catalogue order, whatever order the features were chosen in.
    assert models_mod.missing(cfg, ["semantic", "people"]) == ("people", "semantic")


def test_a_feature_already_downloaded_is_owed_nothing(tmp_path, monkeypatch):
    cfg = Config(cache_dir=str(tmp_path))
    _weights(monkeypatch, people=(True, True), semantic=(True, False))

    assert models_mod.missing(cfg, ["people", "semantic"]) == ("semantic",)


def test_a_backend_this_build_does_not_have_is_not_waiting_on_a_download(tmp_path, monkeypatch):
    """ "Not in this build" is not a download away: onnxruntime is missing, not
    the weights, and its stage already reports itself unavailable."""
    cfg = Config(cache_dir=str(tmp_path))
    _weights(monkeypatch, semantic=(False, False))

    assert models_mod.missing(cfg, ["semantic"]) == ()


def test_the_panel_prices_a_feature_from_the_table_the_fetch_downloads_from(tmp_path, monkeypatch):
    """One table behind both, so the screen cannot quote a download that the
    fetch then decides is unnecessary, or the reverse."""
    cfg = Config(cache_dir=str(tmp_path))
    _weights(monkeypatch, people=(True, True), pets=(True, False), semantic=(False, False))

    catalogue = {f["id"]: f for f in archives_mod.features(cfg)}

    assert catalogue["people"]["ready"] is True and "people" not in models_mod.missing(
        cfg, ["people"]
    )
    assert catalogue["pets"]["ready"] is False and models_mod.missing(cfg, ["pets"]) == ("pets",)
    assert catalogue["semantic"]["available"] is False
    assert catalogue["semantic"]["ready"] is False, "unavailable is not nothing-to-wait-for"
    assert catalogue["places"]["ready"] is True, "a feature with no models is always ready"


# ---------------------------------------------------------------------------
# When the fetch starts
# ---------------------------------------------------------------------------


def test_a_missing_model_is_fetched_as_soon_as_the_archive_is_open(jobs, monkeypatch):
    started = []
    _rig(jobs, monkeypatch, started, states=[_stage("scan")])

    assert jobs.scheduler.tick() is True
    # Before the stages, and alongside them: the download is not work the scan
    # has to finish first, which was the entire complaint.
    assert started == ["models", "scan"]


def test_an_archive_whose_models_are_here_fetches_nothing(jobs, monkeypatch):
    """Weights are shared between archives, so this is the normal case from the
    second archive onwards — and the case that must cost nothing per tick."""
    started = []
    _rig(jobs, monkeypatch, started, missing=(), states=[_stage("scan")])

    jobs.scheduler.tick()

    assert started == ["scan"]


def test_only_one_fetch_runs_at_a_time(jobs, monkeypatch):
    started = []
    _rig(jobs, monkeypatch, started, states=[])
    _running(jobs, models_runner.KIND)

    jobs.scheduler.tick()

    assert started == []


def test_a_paused_pipeline_downloads_nothing(jobs, monkeypatch):
    """ "Pause all" means the app stops using this machine — including its
    network. The fetch is background work like any other."""
    started = []
    _rig(jobs, monkeypatch, started, states=[_stage("scan")])

    jobs.set_paused(True)

    assert jobs.scheduler.tick() is False
    assert started == []


def test_a_failed_fetch_backs_off_further_than_a_stage_does(jobs, monkeypatch):
    """A failed stage has a card saying so; a failed fetch has nowhere to show,
    restarts from zero, and would otherwise retry every two minutes all day."""
    started = []
    _rig(jobs, monkeypatch, started, states=[])
    jobs._error_at[(_ROOT, models_runner.KIND)] = 0.0
    monkeypatch.setattr(jobs.scheduler, "FETCH_COOLDOWN", 10_000_000.0)

    jobs.scheduler.tick()

    assert started == []


# ---------------------------------------------------------------------------
# The fetch is a shortcut, never a gate
# ---------------------------------------------------------------------------


def test_a_stage_waits_for_the_download_of_its_own_weights(jobs, monkeypatch):
    """detect/semantic each fetch their own weights when they start, and neither
    call knows about the other: starting one now would download the same files
    twice, over the same connection."""
    started = []
    _rig(
        jobs,
        monkeypatch,
        started,
        missing=("people", "semantic"),
        states=[_stage("detect"), _stage("semantic")],
    )
    _running(jobs, models_runner.KIND)

    jobs.scheduler.tick()

    assert started == []


def test_a_stage_waits_only_for_the_weights_it_actually_needs(jobs, monkeypatch):
    """Detection and search download disjoint files. Holding face detection back
    for the whole of a 689 MB SigLIP fetch would be an invented dependency — and
    a download that stalls would then stall a stage unrelated to it."""
    started = []
    _rig(jobs, monkeypatch, started, missing=("semantic",), states=[_stage("detect")])
    _running(jobs, models_runner.KIND)

    jobs.scheduler.tick()

    assert started == ["detect"]


def test_the_stages_that_need_no_weights_carry_on_regardless(jobs, monkeypatch):
    started = []
    _rig(
        jobs,
        monkeypatch,
        started,
        missing=("people",),
        states=[_stage("scan"), _stage("places"), _stage("detect")],
    )
    _running(jobs, models_runner.KIND)

    jobs.scheduler.tick()

    assert started == ["scan", "places"]


def test_a_download_in_flight_keeps_the_scheduler_at_its_fast_interval(jobs, monkeypatch):
    """Everything queued behind it starts the moment it lands; backing off to a
    five-minute poll would leave the archive idle for most of that."""
    started = []
    _rig(jobs, monkeypatch, started, missing=(), states=[])
    _running(jobs, models_runner.KIND)

    assert jobs.scheduler.tick() is True


# ---------------------------------------------------------------------------
# What the job itself does
# ---------------------------------------------------------------------------


def _context(cfg, cancel=None):
    job = Job(id=1, kind=models_runner.KIND, root_id=_ROOT, root_path=None)
    ctx = JobContext(
        cfg=cfg,
        job=job,
        cancel=cancel or threading.Event(),
        conn=None,
        log=logging.getLogger("test.models"),
    )
    return ctx, job


def test_the_job_fetches_every_feature_the_archive_still_owes(tmp_path, monkeypatch):
    fetched = []
    monkeypatch.setattr(models_mod, "missing", lambda cfg, enabled: ("people", "semantic"))
    monkeypatch.setattr(
        models_mod, "fetch", lambda cfg, feature_id, log=None: fetched.append(feature_id)
    )
    ctx, job = _context(Config(cache_dir=str(tmp_path)))

    models_runner.run(ctx)

    assert fetched == ["people", "semantic"]
    assert "People" in job.message and "Search by description" in job.message


def test_one_unobtainable_model_does_not_cost_the_others_theirs(tmp_path, monkeypatch):
    """People being unavailable on this machine says nothing about SigLIP."""
    fetched = []

    def fetch(cfg, feature_id, log=None):
        if feature_id == "people":
            raise ModelUnavailableError("no adaface here")
        fetched.append(feature_id)

    monkeypatch.setattr(models_mod, "missing", lambda cfg, enabled: ("people", "semantic"))
    monkeypatch.setattr(models_mod, "fetch", fetch)
    ctx, _job = _context(Config(cache_dir=str(tmp_path)))

    # Reported as a failed job (which is what applies the cooldown), but only
    # after everything else has been tried.
    with pytest.raises(ModelUnavailableError, match="People"):
        models_runner.run(ctx)
    assert fetched == ["semantic"]


def test_the_manager_really_knows_how_to_run_this_kind(jobs, monkeypatch):
    """Through the real registry and a real worker thread, not a stubbed start:
    an unregistered kind fails as "unknown job kind" at the point the download
    should have begun, which no amount of scheduler testing would catch."""
    fetched = []
    monkeypatch.setattr(models_mod, "missing", lambda cfg, enabled: ("pets",))
    monkeypatch.setattr(
        models_mod, "fetch", lambda cfg, feature_id, log=None: fetched.append(feature_id)
    )
    # start() refuses while the scheduler is stopping -- the shutdown guard --
    # and the fixture stopped it so no tick can fire behind a test. That thread
    # has already exited, so clearing the flag starts nothing on its own; it
    # only reopens the door this one test walks through deliberately.
    jobs.scheduler._stopping.clear()

    jobs.start(models_runner.KIND, _ROOT)

    job = wait_until(
        lambda: next((j for j in jobs.list(_ROOT) if j["status"] != "running"), None),
        what="the fetch job to finish",
    )
    assert job["status"] == "done", job["message"]
    assert fetched == ["pets"]


def test_cancelling_is_noticed_inside_a_download(tmp_path, monkeypatch):
    """urlretrieve has no checkpoint of its own; the progress hook is the only
    place inside it that is ours, so that is where the event is checked."""
    cancel = threading.Event()

    def fetch(cfg, feature_id, log=None):
        cancel.set()
        log("downloading search model — 12% of 355 MB")  # the next report hook

    monkeypatch.setattr(models_mod, "missing", lambda cfg, enabled: ("semantic",))
    monkeypatch.setattr(models_mod, "fetch", fetch)
    ctx, _job = _context(Config(cache_dir=str(tmp_path)), cancel=cancel)

    with pytest.raises(KeyboardInterrupt):
        models_runner.run(ctx)


# ---------------------------------------------------------------------------
# What it looks like while it runs
# ---------------------------------------------------------------------------


class _FakeJobs:
    """Enough JobManager for status.snapshot(); stage_states is stubbed away."""

    def __init__(self, current):
        self._current = current

    def list(self, root_id=None):
        return [
            {
                "kind": models_runner.KIND,
                "status": "running",
                "current": self._current,
                "done": 0,
                "total": 0,
                "percent": None,
                "elapsed": 4.0,
                "phase": "working",
            }
        ]

    def paused(self):
        return False

    def paused_stages(self):
        return frozenset()


def test_the_sidebar_shows_the_download_rather_than_a_fixed_line(monkeypatch, tmp_path):
    """It reports no percentage — the sizes are per file, not per feature — so
    the bar is the indeterminate one, and a caption that never changed over it
    is exactly what reads as hung."""
    monkeypatch.setattr(status_mod.stages, "stage_states", lambda *a, **k: [])
    fake = _FakeJobs("downloading search model — 45% of 355 MB")

    snap = status_mod.snapshot(Config(cache_dir=str(tmp_path)), fake, _ROOT, "/fake")

    assert snap["overall"] == "running"
    assert snap["extra"][0]["label"] == "Downloading search model — 45% of 355 MB"
    assert snap["extra"][0]["progress"]["percent"] is None


def test_it_says_what_it_is_before_the_first_byte_arrives(monkeypatch, tmp_path):
    monkeypatch.setattr(status_mod.stages, "stage_states", lambda *a, **k: [])

    snap = status_mod.snapshot(Config(cache_dir=str(tmp_path)), _FakeJobs(""), _ROOT, "/fake")

    assert snap["extra"][0]["label"] == "Downloading models"
