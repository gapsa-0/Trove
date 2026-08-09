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

These drive ``cards.cards``/``status.snapshot`` directly with crafted stage
states, which is the same input the scheduler and the GUI both resolve through.
"""

from __future__ import annotations

from trove.config import Config
from trove.pipeline import cards as cards_mod
from trove.pipeline import stages as stages_mod
from trove.pipeline import status as status_mod


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
    cards = {c["id"]: c for c in cards_mod.cards(list(states))}
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
    setup total as "Fingerprinting photos…" -- a loop claim made before the
    loop exists."""
    prep = _progress(phase="preparing", done=0, total=40000, current="")

    card = _card(_stage(kind="dedup", card="dedup", counted=False, progress=prep))

    assert card["progress"] is None
    assert card["message"] == "Preparing…"


def test_the_fingerprinting_line_leaves_the_counts_to_the_detail_line():
    """It used to read "Fingerprinting 22,900 of 88,274 photos…" directly above
    a detail line reading "22,900/88,274", printing the same pair twice. Every
    other card's running text is a verb and a noun; this one now matches."""
    running = _progress(done=22_900, total=88_274, current="2019/IMG_1.jpg")

    card = _card(_stage(kind="dedup", card="dedup", counted=False, progress=running))

    assert card["message"] == "Fingerprinting photos…"
    # The numbers are still on their way to the client, once.
    assert (card["progress"]["done"], card["progress"]["total"]) == (22_900, 88_274)


def test_the_grouping_phase_still_names_itself():
    grouping = _progress(done=40, total=900, current="12× exact")

    card = _card(_stage(kind="dedup", card="dedup", counted=False, progress=grouping))

    assert card["message"] == "Grouping duplicates…"


def test_searching_for_near_duplicates_does_not_claim_to_be_fingerprinting():
    """Dedup grew a third phase between the other two: comparing fingerprints
    against the archive (dedup/edges.py). It reports an empty `current` on
    purpose -- `current` is both the card's detail line and its signal that a
    *photo* is being decoded, which is a different and far slower phase. The
    accurate flat text stands instead, over a bar that moves."""
    searching = _progress(done=1_200, total=88_234, current="")

    card = _card(_stage(kind="dedup", card="dedup", counted=False, progress=searching))

    assert card["message"] == "Finding duplicates…"
    assert (card["progress"]["done"], card["progress"]["total"]) == (1_200, 88_234)


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
# Crossing files the walk already holds, before it reaches anything new
# ---------------------------------------------------------------------------


def test_a_scan_crossing_files_it_already_has_draws_no_bar():
    """Scan is the only stage that restarts at the top of the tree rather than
    at its own backlog, and re-reaching what it holds costs a stat() per file.
    Counted as progress, that made the bar rewind to 0 and race back to where
    it stopped -- work being redone, apparently, when none is.

    The sentence in its place says why the walk is there: crossing known files
    is how an edit is spotted and how a deletion is, not repetition. It used to
    read "Re-checking 4,000 files already scanned…", which named the activity,
    gave no reason for it, and spent two words insisting it was a second pass.
    """
    crossing = _progress(done=4000, total=30772, recheck_below=12400, current="2019/a.jpg")

    card = _card(_stage(kind="scan", card="scan", progress=crossing))

    assert card["progress"] is None
    assert card["message"] == "Checking 4,000 files for changes…"


def test_the_bar_comes_back_where_the_scan_left_off():
    """The point of the phase: the bar's first appearance is at the mark, not
    at 0, so it starts from where the interrupted run stopped."""
    arrived = _progress(done=12400, total=30772, recheck_below=12400)

    card = _card(_stage(kind="scan", card="scan", progress=arrived))

    assert card["progress"]["done"] == 12400
    assert card["message"] == "Scanning files…"


def test_a_first_scan_has_nothing_to_re_check():
    """`recheck_below` is 0 on an empty catalogue, and on every stage that
    resumes at its own backlog -- so this costs them nothing."""
    fresh = _progress(done=0, total=30772, recheck_below=0)

    card = _card(_stage(kind="scan", card="scan", progress=fresh))

    assert card["progress"] is not None
    assert card["message"] == "Scanning files…"


def test_preparing_outranks_re_checking():
    """The mark is recorded while the disk is being counted, so it is already
    set before the walk starts -- and a scan that has not opened a file yet is
    not re-reading one."""
    counting = _progress(phase="preparing", done=0, recheck_below=12400, current="counting files")

    card = _card(_stage(kind="scan", card="scan", progress=counting))

    assert card["progress"] is None
    assert card["message"] == "Preparing… · counting files"


def test_a_re_checking_scan_says_that_dating_files_is_still_running():
    """Dating files runs parallel to the walk, so this scan usually shares its
    card with a stage doing real work. A sentence about the walk alone had the
    Overview claiming nothing was happening while its own "With a date" tile
    climbed on the same poll."""
    crossing = _progress(done=4000, total=30772, recheck_below=12400)

    card = _card(
        _stage(kind="scan", card="scan", progress=crossing),
        _stage(kind="enrich", card="scan", state="running"),
    )

    assert card["progress"] is None, "the two count different things; neither bar fits here"
    assert card["message"] == "Checking 4,000 files for changes · reading metadata"


def test_a_re_checking_scan_alone_claims_nothing_about_metadata():
    crossing = _progress(done=4000, total=30772, recheck_below=12400)

    card = _card(
        _stage(kind="scan", card="scan", progress=crossing),
        _stage(kind="enrich", card="scan", state="up_to_date"),
    )

    assert card["message"] == "Checking 4,000 files for changes…"


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


# ---------------------------------------------------------------------------
# Before the disk has been counted: what the card must not claim
# ---------------------------------------------------------------------------


def test_a_scan_that_has_not_counted_the_disk_yet_says_so():
    """The polled status endpoint no longer waits for the disk walk, so a
    freshly opened archive answers before anyone knows how much is on disk.

    The card that used to be drawn from that gap said "up to date", because a
    backlog nobody had measured came through as zero -- an archive announced
    fully indexed before anything had looked at it, which is the one wrong
    answer a user has no reason to doubt and will act on.
    """
    card = _card(
        _stage(kind="scan", card="scan", state="checking", pending=None, progress=None),
        _stage(kind="enrich", card="scan", state="up_to_date", pending=0, progress=None),
    )
    assert card["state"] == "checking"
    assert card["pending"] is None, "reported a backlog it has not measured"
    # Not "Counting files in this folder…", which is how the question gets
    # answered rather than what was asked -- and which, landing on the Indexing
    # card, reads as indexing doing its own job a second time.
    assert card["message"] == "Checking for work…"


def test_an_uncounted_scan_outranks_a_finished_stage_on_the_same_card():
    """Scan and enrich share the Scan card, and the roll-up takes the most
    active member. "Not looked yet" has to beat "nothing to do" or the fused
    card reports the very reassurance the state exists to withhold."""
    assert cards_mod._STATE_RANK["checking"] > cards_mod._STATE_RANK["up_to_date"]


def test_an_archive_still_counting_is_not_reported_idle(monkeypatch):
    """`overall` drives the sidebar chip, which is on screen everywhere. Idle
    there is "Up to date" -- the same false all-clear, in the one place the
    user sees from every section."""
    snap = _snapshot(
        monkeypatch,
        _FakeJobs(),
        _stage(kind="scan", card="scan", state="checking", pending=None, progress=None),
    )
    assert snap["overall"] == "checking"


def test_known_work_is_reported_over_a_count_that_has_not_landed(monkeypatch):
    """Checking is the last answer, not the first: if anything is known to be
    outstanding, saying so is more use than reporting the count still in
    flight."""
    snap = _snapshot(
        monkeypatch,
        _FakeJobs(),
        _stage(kind="scan", card="scan", state="checking", pending=None, progress=None),
        _stage(kind="places", card="places", state="queued", pending=12, progress=None),
    )
    assert snap["overall"] == "working"
