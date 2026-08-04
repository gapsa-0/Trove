"""What a status card says while a stage is starting up.

Between "the stage I depend on finished" and "I am processing file 1" a stage
can spend minutes counting 150k files or fetching ~310 MB of model weights. The
card painted a 0% progress bar across all of it -- a bar whose total nothing had
started consuming -- and the download's own percentage was relegated to the
small detail line beside it.
These drive ``stages.cards`` directly with crafted stage states, which is the
same input the scheduler and the GUI both resolve through.
"""

from __future__ import annotations

from organize_archive.pipeline import stages as stages_mod


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
        "blocker": None,
        "error": None,
    }
    s.update(over)
    return s


def _card(*states, card_id=None):
    cards = {c["id"]: c for c in stages_mod.cards(list(states))}
    return cards[card_id or states[0]["card"]]


# ---------------------------------------------------------------------------
# Preparing: no bar, and the setup explains itself
# ---------------------------------------------------------------------------


def test_a_preparing_stage_shows_no_bar_and_leads_with_what_it_is_doing():
    """The first-run case this exists for: a 249 MB download reported as a
    stalled 12% bar next to "Detecting people & pets…"."""
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
    assert card["message"] == "Detecting people & pets…"


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
