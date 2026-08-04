"""What a status card says while a stage is starting up or stopping.

Both ends of a stage's life used to be reported as if it were mid-loop:

* Between "the stage I depend on finished" and "I am processing file 1" a stage
  can spend minutes counting 150k files or fetching ~310 MB of model weights.
  The card painted a 0% progress bar across all of it -- a bar whose total
  nothing had started consuming -- and the download's own percentage was
  relegated to the small detail line beside it.
* Pausing only *asks* the running job to stop, at its next batch checkpoint, so
  the card went on saying "Scanning files…" for seconds and then stopped dead;
  and once it had stopped, the bar vanished, losing how far in it got.

These drive ``stages.cards``/``status.snapshot`` directly with crafted stage
states, which is the same input the scheduler and the GUI both resolve through.
"""

from __future__ import annotations

from organize_archive.config import Config
from organize_archive.pipeline import stages as stages_mod
from organize_archive.pipeline import status as status_mod


def _progress(**over):
    p = {"percent": 12.0, "done": 600, "total": 5000, "current": "", "elapsed": 8.0}
    p.update(over)
    return p


def _stage(kind="detect", card="detect", state="running", **over):
    s = {
        "kind": kind,
        "card": card,
        "counted": True,
        "state": state,
        "pending": 4400,
        "progress": _progress() if state == "running" else None,
        "stopped_progress": None,
        "blocker": None,
        "error": None,
    }
    s.update(over)
    return s


def _card(*states, card_id=None):
    cards = {c["id"]: c for c in stages_mod.cards(list(states))}
    return cards[card_id or states[0]["card"]]


class _FakeJobs:
    """Enough JobManager for snapshot(); stage_states is monkeypatched away."""

    def __init__(self, paused=False, stages=()):
        self._paused, self._stages = paused, set(stages)

    def paused(self):
        return self._paused

    def paused_stages(self):
        return set(self._stages)

    def list(self, root_id=None):
        return []


def _snapshot(monkeypatch, jobs, *states):
    monkeypatch.setattr(
        stages_mod,
        "stage_states",
        lambda cfg, jobs, root_id, root_path, allow_walk=False: [dict(s) for s in states],
    )
    return status_mod.snapshot(Config(), jobs, 1, "/fake")


# ---------------------------------------------------------------------------
# Preparing: no bar, and the setup explains itself
# ---------------------------------------------------------------------------


def test_a_preparing_stage_shows_no_bar_and_leads_with_what_it_is_doing():
    """The first-run case this exists for: a 249 MB download reported as a
    stalled 12% bar next to "Finding people & pets…"."""
    downloading = _progress(phase="preparing", current="downloading adaface model — 42% of 249 MB")

    card = _card(_stage(progress=downloading))

    assert card["progress"] is None
    assert card["message"] == "Preparing… · downloading adaface model — 42% of 249 MB"


def test_a_preparing_stage_with_nothing_to_report_still_says_preparing():
    card = _card(_stage(progress=_progress(phase="preparing", current="")))

    assert card["progress"] is None
    assert card["message"] == "Preparing…"


def test_a_setup_line_written_for_a_log_does_not_bring_its_own_ellipsis():
    prep = _progress(phase="preparing", current="downloading face models (buffalo_l) …")

    card = _card(_stage(progress=prep))

    assert card["message"] == "Preparing… · downloading face models (buffalo_l)"


def test_the_bar_and_the_running_text_come_back_once_the_loop_starts():
    card = _card(_stage(progress=_progress(phase="working", current="2019/IMG_1.jpg")))

    assert card["progress"]["done"] == 600
    assert card["message"] == "Finding people & pets…"


def test_a_preparing_dedup_does_not_claim_to_be_fingerprinting():
    """dedup's card infers its phase from the progress shape, which reads a
    setup total as "Fingerprinting 0 of 40,000 photos…" -- a loop claim made
    before the loop exists."""
    prep = _progress(phase="preparing", done=0, total=40000, current="")

    card = _card(_stage(kind="dedup", card="dedup", counted=False, progress=prep))

    assert card["progress"] is None
    assert card["message"] == "Preparing…"


def test_a_preparing_scan_is_not_reported_as_finalizing_metadata():
    """The Scan card fuses scan ∥ enrich and has its own bar rule; preparing
    still wins, because neither stage is walking files yet."""
    prep = _progress(phase="preparing", current="counting files on disk")

    card = _card(
        _stage(kind="scan", card="scan", progress=prep),
        _stage(kind="enrich", card="scan", state="queued"),
    )

    assert card["progress"] is None
    assert card["message"] == "Preparing… · counting files on disk"


# ---------------------------------------------------------------------------
# Pausing: the wind-down, and the bar a stopped run leaves behind
# ---------------------------------------------------------------------------


def test_a_stage_winding_down_says_pausing_and_keeps_its_bar(monkeypatch):
    snap = _snapshot(monkeypatch, _FakeJobs(stages={"detect"}), _stage())

    card = next(c for c in snap["stages"] if c["id"] == "detect")
    assert card["state"] == "running"  # the job really is still going
    assert card["pausing"] is True
    assert card["message"] == "Pausing…"
    assert card["progress"]["done"] == 600, "the bar must not vanish while work continues"


def test_the_whole_pipeline_pause_puts_every_running_card_into_pausing(monkeypatch):
    snap = _snapshot(
        monkeypatch,
        _FakeJobs(paused=True),
        _stage(kind="scan", card="scan"),
        _stage(),
    )

    assert [c["message"] for c in snap["stages"]] == ["Pausing…", "Pausing…"]


def test_a_stage_nobody_paused_keeps_its_running_text(monkeypatch):
    snap = _snapshot(monkeypatch, _FakeJobs(stages={"scan"}), _stage())

    card = next(c for c in snap["stages"] if c["id"] == "detect")
    assert card["pausing"] is False
    assert card["message"] == "Finding people & pets…"


def test_a_stage_stopped_mid_run_keeps_the_bar_it_reached(monkeypatch):
    """Paused work is suspended, not discarded: it resumes from the batches it
    committed, so "Paused" alone throws away the only interesting number."""
    stopped = _stage(state="queued", stopped_progress=_progress(phase="working"))

    snap = _snapshot(monkeypatch, _FakeJobs(stages={"detect"}), stopped)

    card = next(c for c in snap["stages"] if c["id"] == "detect")
    assert card["message"] == "Paused"
    assert card["progress"]["done"] == 600


def test_a_queued_stage_that_is_not_paused_shows_no_leftover_bar(monkeypatch):
    """The frozen bar is a pause affordance; a stage merely waiting its turn
    must not show one, or every restart would look mid-run."""
    stopped = _stage(state="queued", stopped_progress=_progress(phase="working"))

    snap = _snapshot(monkeypatch, _FakeJobs(), stopped)

    card = next(c for c in snap["stages"] if c["id"] == "detect")
    assert card["progress"] is None
    assert "stopped_progress" not in card, "internal-only; never reaches the client"


# ---------------------------------------------------------------------------
# Which finished job counts as "stopped mid-run"
# ---------------------------------------------------------------------------


def test_only_a_cancelled_job_leaves_a_bar_behind():
    cancelled = {"status": "cancelled", "done": 600, "total": 5000, "phase": "working"}
    assert stages_mod._stopped_progress(cancelled)["done"] == 600

    for status in ("done", "error", "running"):
        assert stages_mod._stopped_progress({**cancelled, "status": status}) is None


def test_a_job_cancelled_while_preparing_leaves_no_bar():
    """It never reached a count worth showing, and 0/0 only looks broken."""
    assert (
        stages_mod._stopped_progress(
            {"status": "cancelled", "done": 0, "total": 0, "phase": "preparing"}
        )
        is None
    )
