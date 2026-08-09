"""Rolling resolved stage states up into the cards the Overview renders.

``stages.py`` decides what state every stage is in. This is the half that turns
those states into display cards: which stage leads a card that several share,
what the card says, which bar it draws, and which blocked card is next in line.
It decides nothing the scheduler could disagree with -- every input is a state
``stage_states`` already resolved.

The split is one-directional on purpose. This module reads the stage table
(``CARD_ORDER``, ``_CARD_OF``, the two kinds the Scan card fuses) from
``stages.py``; nothing there imports back, which is what keeps a card's wording
out of the path the scheduler walks.

Every name and mark on a card comes from ``features.py``, composed per card from
the features the archive actually enabled -- there is deliberately no table of
card names anywhere in this package.
"""

from __future__ import annotations

from typing import Any

from .. import features
from .stages import _CARD_OF, CARD_ORDER, ENRICH, SCAN

# The one string with nowhere better to live: an unavailable stage is one whose
# optional dependency will not import, which is the same condition the setup
# panel reports on the feature's own card, in these words.
_UNAVAILABLE_TEXT = "Not in this build"

# Precedence when several stages share one card (scan + enrich): the most
# "active" state wins, so a running enrich keeps the Scan card spinning.
_STATE_RANK = {
    "running": 6,
    "error": 5,
    "queued": 4,
    "blocked": 3,
    # Above up_to_date because it is the one thing a card must not round down
    # to: "we have not looked yet" and "there is nothing to do" are opposite
    # claims, and only one of them is safe to be wrong about.
    "checking": 2,
    "unavailable": 1,
    "up_to_date": 0,
}


def _scan_card_progress(
    members: list[dict], progress: Any, message: str | None
) -> tuple[Any, str | None]:
    """The Scan card's bar, which two parallel stages must not share.

    Scan is the only card that fuses two stages running at once (scan ∥ enrich),
    and they count different things. Reusing one bar across both makes the fill
    shoot past 100% and then rewind when the source flips. So the bar shows
    *only while scanning*; once the on-disk walk is done but metadata extraction
    is still catching up, drop the bar and say so plainly.

    A scan re-crossing ground it already covered is a third case, and the one
    that made this the right home for the decision rather than the tail of
    ``_card``: it has no bar worth drawing, but the stage beside it usually
    does have work in flight, and only here are both members in view to say so.
    Handing the bar to enrich instead is the one thing not on offer -- their
    totals are unrelated, which is the whole reason this function exists.
    """
    by_kind = {m["kind"]: m for m in members}
    enriching = by_kind.get(ENRICH, {}).get("state") == "running"
    if by_kind.get(SCAN, {}).get("state") == "running":
        scan_progress = by_kind[SCAN]["progress"]
        if scan_progress and _rechecking(scan_progress):
            return None, _recheck_message(scan_progress, enriching)
        return scan_progress, message
    if enriching:
        return None, "Finalizing metadata extraction…"
    return progress, message


def _dedup_card_message(progress: dict, message: str | None) -> str | None:
    """Which of dedup's two phases is running, inferred from its progress shape.

    Dedup is `counted=False` (a wholesale rebuild has no per-file backlog), so
    it would otherwise sit on the flat "Finding duplicates…" text for the whole
    run. It actually has two real phases sharing one job's progress:
    `_perceptual_hashes` fingerprints images (current=rel_path), then `run()`'s
    grouping loop unions them into groups (current="<n>× exact/perceptual").
    Telling them apart from that shape beats threading a phase flag through
    jobs.py.

    `done > total` catches the instant the grouping phase starts, before its own
    first progress update: `total` has already flipped from the image count to
    the (usually smaller) group count while `current`/`done` still hold the
    fingerprinting pass's final values.
    """
    current = progress.get("current") or ""
    total = progress.get("total") or 0
    done = progress.get("done") or 0
    if "×" in current or (total and done > total):
        return "Grouping duplicates…"
    if total and current and done < total:
        # `current` is only a photo path while fingerprinting is actually
        # running. Without it (no imagehash installed, so `run()` skips
        # straight to grouping and sets `total` itself) claiming to
        # fingerprint would be a lie; the flat text stands instead.
        #
        # No counts in the text. Every other card's running line is "<verb>
        # <noun>…" from features.card_running, and the numbers live once, in
        # the detail line the client draws directly underneath -- this one used
        # to say "Fingerprinting 22,900 of 88,274 photos…" immediately above a
        # line reading "22,900/88,274".
        return "Fingerprinting photos…"
    return message


def _rechecking(progress: dict) -> bool:
    """Whether this run is still crossing ground it has already covered.

    True only for a scan that stopped part-way and started again from the top
    of the tree (see ``Job.recheck_below``). It is the same situation
    ``preparing`` describes -- the stage is busy, and a bar over it would be
    counting something other than the work -- so the card treats it the same
    way, with its own sentence.

    A stage still preparing is *not* re-checking, whatever mark it has already
    recorded: the mark is set while the disk is being counted, and a scan that
    has not opened a file yet cannot be re-reading one. Both phases hide the
    bar, so this only decides which sentence is true.
    """
    if progress.get("phase") == "preparing":
        return False
    return (
        bool(progress.get("recheck_below"))
        and (progress.get("done") or 0) < progress["recheck_below"]
    )


def _recheck_message(progress: dict, enriching: bool) -> str:
    """No denominator on purpose. How many files are being re-checked is
    knowable, but saying "12,400 of 30,772" reads as a bar written out in
    words, and it is the number this phase exists to stop drawing.

    ``enriching`` is the other half of this card. Dating files runs parallel to
    the walk, so a re-checking scan is very often sharing the card with a stage
    doing real work -- and saying only "Re-checking…" left the Overview
    claiming nothing was happening while its own "With a date" tile climbed.
    """
    done = progress.get("done") or 0
    crossing = f"Re-checking {done:,} files already scanned"
    return f"{crossing} · reading metadata" if enriching else f"{crossing}…"


def _preparing_message(progress: dict) -> str:
    """What a stage says between starting and reaching its first file: whatever
    its setup last reported (see ``JobContext.preparing``), or nothing.

    The reports are written for a log line and several trail an ellipsis of
    their own ("downloading face models (buffalo_l) …"), which behind this
    prefix would be the second one in six words. One is enough.
    """
    detail = (progress.get("current") or "").strip().rstrip("…. ")
    return f"Preparing… · {detail}" if detail else "Preparing…"


def _card(
    card_id: str,
    members: list[dict],
    blocker_card: dict[str, str | None],
    enabled: tuple[str, ...],
) -> dict:
    """Roll one card's member stages into the dict the GUI renders."""
    lead = max(members, key=lambda s: _STATE_RANK[s["state"]])
    state = lead["state"]
    counted = any(m["counted"] for m in members)
    counts = [m["pending"] for m in members if m["counted"]]
    # One member with nothing to report makes the card's total unreportable --
    # summing the rest would print a number smaller than the truth and look
    # like the whole answer. See stages: only scan, only before its first walk.
    pending = sum(counts) if counted and all(c is not None for c in counts) else None
    progress = next((m["progress"] for m in members if m["progress"]), None)
    blocker = lead["blocker"]
    blocker_card[card_id] = _CARD_OF.get(blocker) if blocker else None
    message = _message(card_id, state, pending, blocker, lead.get("error"), enabled)

    if card_id == "scan":
        progress, message = _scan_card_progress(members, progress, message)
    elif card_id == "dedup" and state == "running" and progress:
        message = _dedup_card_message(progress, message)
    # Last, so it outranks the wording above: "Finding duplicates…" and
    # "Fingerprinting photos…" are both claims about a loop that
    # has not started yet. (Re-checking is the same kind of claim, but it is
    # decided in _scan_card_progress, where the card's other stage is visible.)
    if progress and progress.get("phase") == "preparing":
        message = _preparing_message(progress)
        progress = None

    bc_id = blocker_card[card_id]
    return {
        "id": card_id,
        "label": features.card_label(card_id, enabled),
        # A key into the frontend's ICONS, not a drawing: the same mark this
        # feature carries on its setup card and (where it has one) its nav
        # section, so the card watching the work is recognisably the card that
        # asked for it.
        "icon": features.card_icon(card_id, enabled),
        # Where this card sits in the chain: part of the trunk every archive
        # runs, or something clipped onto it. The Overview's rail draws the
        # two differently -- see features.card_always_runs.
        "always_runs": features.card_always_runs(card_id),
        "state": state,
        "pending": pending,
        "counted": counted,
        "progress": progress,
        # Promoted to `progress` by _apply_pause, which knows whether this card
        # is paused rather than merely queued, and dropped by it otherwise.
        "stopped_progress": next(
            (m.get("stopped_progress") for m in members if m.get("stopped_progress")), None
        ),
        "next": False,
        # True while a paused stage's job is still winding down to its next
        # batch checkpoint -- set by snapshot, which knows about the pause.
        "pausing": False,
        "waiting_on": features.card_label(bc_id, enabled) if bc_id else None,
        "message": message,
    }


def _mark_up_next(result: list[dict], blocker_card: dict[str, str | None]) -> None:
    """A blocked card is *next in line* when the stage it waits on is running now.

    It is the one that starts the moment the current work finishes. Flagging it
    (the GUI colours it and swaps the flat "Waiting for …" line for a "goes
    next" line) makes the queue read as an ordered pipeline rather than a wall
    of identical "waiting" cards.
    """
    running_cards = {c["id"] for c in result if c["state"] == "running"}
    # Read back off the cards rather than recomposed: a blocker is always a
    # card that was built (it is a dependency, and those are required
    # features), and taking its name from the card itself is what guarantees
    # the two say the same thing.
    label = {c["id"]: c["label"] for c in result}
    for c in result:
        bc = blocker_card.get(c["id"])
        if c["state"] == "blocked" and bc is not None and bc in running_cards:
            c["next"] = True
            c["message"] = f"Up next · after {label[bc]}"


def _mark_stalled(
    result: list[dict],
    blocker_card: dict[str, str | None],
    paused_stages: frozenset[str] | set[str],
) -> None:
    """Propagate the pause flags down each dependency chain.

    CARD_ORDER is dependency-ordered, so a card's blocker has already been
    resolved by the time we reach it and one forward walk is enough.
    """
    stalled: dict[str, bool] = {}
    for c in result:
        bc = blocker_card.get(c["id"])
        c["paused"] = c["id"] in paused_stages
        c["stalled"] = c["paused"] or bool(bc and stalled.get(bc))
        stalled[c["id"]] = c["stalled"]


def cards(
    states: list[dict],
    paused_stages: frozenset[str] | set[str] = frozenset(),
    enabled: tuple[str, ...] = (),
) -> list[dict]:
    """Roll per-stage states up into the display cards the GUI renders verbatim.

    ``paused_stages`` holds the card ids the user paused individually. A card
    carries two flags for it: ``paused`` (this card's own toggle, what its
    button reflects) and ``stalled`` (it cannot progress — either it is paused
    or everything it waits on is). Only ``stalled`` may be used to decide that
    no work is coming, since a card blocked behind a paused stage is just as
    stopped as the paused one itself.

    A card with no member stages is simply not built, which is how a feature the
    archive does not run disappears from the Overview: ``stage_states`` already
    left its stages out.

    ``enabled`` is what every word and mark on a card is composed from: its
    name, its running line, its icon, and the name of whatever a blocked card
    is waiting for all come from the feature catalogue, so a card describes the
    work this archive asked for and no other ("People", not "People & pets", on
    an archive that never wanted pets).
    """
    by_card: dict[str, list[dict]] = {}
    for s in states:
        by_card.setdefault(s["card"], []).append(s)

    blocker_card: dict[str, str | None] = {}
    result = [
        _card(card_id, by_card[card_id], blocker_card, enabled)
        for card_id in CARD_ORDER
        if by_card.get(card_id)
    ]
    _mark_up_next(result, blocker_card)
    _mark_stalled(result, blocker_card, paused_stages)
    return result


def _message(
    card_id: str,
    state: str,
    pending: int | None,
    blocker: str | None,
    error: str | None,
    enabled: tuple[str, ...],
) -> str | None:
    """Fixed wording the client shows for every non-terminal state. The
    up_to_date ("done") message is left to the client, which already holds the
    per-domain summary numbers (duplicate count, faces found, …).

    ``enabled`` is what lets a card speak about the work this archive asked
    for: both the running line and the name of whatever a blocked card waits
    on are composed from the feature set (see ``features.card_running``)."""
    if state == "running":
        return features.card_running(card_id, enabled)
    if state == "blocked":
        blocker_card = _CARD_OF.get(blocker) if blocker else None
        waiting = features.card_label(blocker_card, enabled) if blocker_card else "earlier steps"
        return f"Waiting for {waiting}…"
    if state == "queued":
        if pending and pending > 0:
            noun = "item" if pending == 1 else "items"
            return f"{pending:,} {noun} queued"
        return "Queued"
    if state == "checking":
        # The same words the panel uses before its first snapshot lands
        # (overview.js), because it is the same thing: deciding whether any
        # stage has anything to do.
        #
        # It used to name the mechanism -- "Counting files in this folder…" --
        # on the theory that a card sitting for twenty seconds should say what
        # it is waiting for. But counting files is how the question is
        # answered, not what the user asked, and this line lands on the
        # *Indexing* card, directly after a stage whose whole job is to go
        # through the files in that folder. Read from outside the code it says
        # the app is doing the same work again. What makes a wait read as a
        # hang is missing motion, not missing jargon, and the spinner beside
        # this is what answers that.
        return "Checking for work…"
    if state == "unavailable":
        return _UNAVAILABLE_TEXT
    if state == "error":
        return error or "Last run failed, will retry"
    return None
