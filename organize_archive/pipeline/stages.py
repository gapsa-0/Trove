"""Single source of truth for pipeline stage status.

Both the scheduler (deciding what to start next) and the GUI (deciding what to
render) read the *same* resolved stage list from here, so a status card can
never disagree with what the pipeline is actually doing.

A stage's state is derived, in one place, from two things:

* **countable pending work in the catalog**, a file with no ``dates`` row *is*
  enrich-pending; a canonical image either detector still owes work *is*
  detect-pending.
  Nothing about "what's left to do" is persisted separately, because the catalog
  already implies it and re-derives it for free on restart.
* **the live in-process jobs**, whether a worker of that kind is running right
  now, and whether the last one errored.

Every stage resolves through the SAME rule
(``unavailable → error → running → blocked → queued → up_to_date``), which is why
the five cards finally behave identically instead of each computing its own truth.

``status.py`` sits on top of this with the part only the GUI needs -- the pause
overlay, the jobs that belong to no card, one overall verdict. It decides no
state of its own, so there is nothing there for the scheduler to disagree with.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..config import Config
from ..db import database as db

if TYPE_CHECKING:
    # TYPE_CHECKING-only: manager.py imports this module (via scheduler.py at
    # runtime, locally to dodge the cycle -- see manager.py's own docstring),
    # so a real import here would close the loop. Safe under `from __future__
    # import annotations`: the annotation is never evaluated at runtime.
    from .manager import JobManager

# Stage kinds (also the job ``kind`` values the worker dispatches on).
SCAN, ENRICH, DEDUP, PLACES, DETECT, SEMANTIC = (
    "scan",
    "enrich",
    "dedup",
    "places",
    "detect",
    "semantic",
)

# Stages that take the single DB-writer lock run one at a time; the rest use
# their own connection and overlap freely (scan ∥ enrich ∥ semantic).
LOCK_KINDS = frozenset({DEDUP, PLACES, DETECT})
PARALLEL_KINDS = frozenset({SCAN, ENRICH, SEMANTIC})


@dataclass(frozen=True)
class StageDef:
    kind: str
    deps: tuple[str, ...]  # upstream kinds that must be up_to_date first
    card: str  # which display card this stage rolls up into
    counted: bool  # whether `pending` is a number worth showing


# Order matters: dependency resolution and the scheduler both walk this list in
# order, so a stage's deps always appear before it.
STAGES: tuple[StageDef, ...] = (
    StageDef(SCAN, (), "scan", True),
    StageDef(ENRICH, (), "scan", True),  # runs parallel to scan
    StageDef(DEDUP, (SCAN, ENRICH), "dedup", False),  # wholesale rebuild, no count
    StageDef(PLACES, (DEDUP,), "places", True),
    StageDef(DETECT, (DEDUP,), "detect", True),  # people + pets, one decode
    StageDef(SEMANTIC, (DEDUP,), "semantic", True),
)

# Display cards, in the order the Overview renders them.
CARD_ORDER = ("scan", "dedup", "detect", "places", "semantic")
# Coherent operation names (one per card), in a single consistent format.
CARD_LABEL = {
    "scan": "Scan",
    "dedup": "Deduplication",
    "detect": "People & pets",
    "places": "Location mapping",
    "semantic": "Semantic indexing",
}
# What a card says while its stage is actively running, one consistent
# "<verb>ing <object>…" format across every stage.
_RUN_TEXT = {
    "scan": "Scanning files…",
    "dedup": "Finding duplicates…",
    "detect": "Detecting people & pets…",
    "places": "Mapping locations…",
    "semantic": "Indexing media…",
}
_UNAVAILABLE_TEXT = {
    "detect": "Detection unavailable",
    "semantic": "Semantic indexing unavailable",
}


def _availability(cfg: Config) -> dict[str, bool]:
    from ..detect import extract as dx
    from ..services import semantic

    # Both of these ask "are the dependencies importable", never "are the model
    # weights on disk": an unavailable stage is never queued, and it is the
    # stage itself that downloads its weights, so gating on the files would be
    # a deadlock.
    return {
        SCAN: True,
        ENRICH: True,
        DEDUP: True,
        PLACES: True,
        DETECT: dx.available(),
        SEMANTIC: semantic.available(),
    }


def _pending(
    cfg: Config,
    jobs: JobManager,
    root_id: int,
    root_path: str,
    avail: dict[str, bool],
    allow_walk: bool,
) -> dict[str, int]:
    """Countable backlog per stage, from the catalog. One connection for the
    cheap DB counts; the expensive disk walk is served from the manager's cache."""
    from ..pets.extract import scan_source as pet_scan_source

    # Imported unqualified rather than module-qualified (as in server.py):
    # this function has local variables named `pending` below, which a
    # `from ..services import pending` import would shadow.
    from ..services.pending import detect_pending
    from ..services.search import semantic_pending

    db_path = cfg.archive_db_path(root_id)
    # The disk walk is the expensive half and must happen outside the read
    # connection, so a slow drive never holds one open across the whole query.
    on_disk = jobs.disk_count(root_id, root_path, allow_walk=allow_walk)
    conn = db.open_readonly(db_path)
    try:
        settled = db.scan_settled(conn, root_id, on_disk)
        indexed = conn.execute(
            "SELECT COUNT(*) FROM files f WHERE f.present=1 AND f.root_id=?", (root_id,)
        ).fetchone()[0]
        enriched = conn.execute(
            """SELECT COUNT(*) FROM files f JOIN dates d ON d.file_id=f.id
               WHERE f.present=1 AND f.root_id=?""",
            (root_id,),
        ).fetchone()[0]
        # Geotagged files not yet attached to any place (covers the first-time
        # bootstrap too: with no clusters yet, every geotagged file is unplaced).
        geo_unplaced = conn.execute(
            """SELECT COUNT(*) FROM files f JOIN geo g ON g.file_id=f.id
               WHERE f.present=1 AND f.root_id=? AND g.lat IS NOT NULL
                 AND f.id NOT IN (SELECT file_id FROM place_cluster_members)""",
            (root_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    # What is left to scan is the difference between disk and catalog — except
    # that difference never quite closes if any file cannot be read, so it is
    # only consulted while the last completed scan does *not* already account
    # for what is on disk now (db.scan_settled). Without that the scheduler
    # relaunches the scan the instant it finishes, forever. The floor of 1 keeps
    # a deletion-only change (fewer files on disk than rows) visible as work.
    # Kept as a block rather than the ternary ruff suggests: as one line it ends
    # in `... or 1`, where `or` is a default and the `or` in the condition is a
    # boolean, and the floor stops looking deliberate.
    if settled or on_disk is None:  # noqa: SIM108
        new_files = 0
    else:
        new_files = max(0, on_disk - indexed) or 1

    return {
        SCAN: new_files,
        ENRICH: max(0, indexed - enriched),
        # Dedup rebuilds wholesale, so it has no per-file backlog, a dirty flag,
        # set when scan/enrich change data and cleared on a successful rebuild.
        DEDUP: 1 if jobs.dedup_needed(root_id) else 0,
        PLACES: geo_unplaced,
        DETECT: (
            detect_pending(db_path, root_id, pet_scan_source(cfg), cfg.detect_video_frames)
            if avail[DETECT]
            else 0
        ),
        SEMANTIC: (semantic_pending(db_path, root_id) if avail[SEMANTIC] else 0),
    }


def stage_states(
    cfg: Config, jobs: JobManager, root_id: int, root_path: str, allow_walk: bool = False
) -> list[dict[str, Any]]:
    """Resolve every stage to one state, used by BOTH the scheduler and the API.

    Returns one dict per stage (not per card) so the scheduler can act on the
    fine-grained scan/enrich split; ``cards()`` rolls them up for display.

    ``allow_walk`` is for the scheduler, which runs on its own thread and can
    afford to wait for a fresh disk count; the polled API path must not (see
    JobManager.disk_count).
    """
    avail = _availability(cfg)
    pending = _pending(cfg, jobs, root_id, root_path, avail, allow_walk)

    running: dict[str, dict] = {}
    last: dict[str, dict] = {}
    for j in jobs.list(root_id):  # newest first
        last.setdefault(j["kind"], j)
        if j["status"] == "running":
            running.setdefault(j["kind"], j)

    resolved: dict[str, str] = {}
    out: list[dict] = []
    for sd in STAGES:
        k = sd.kind
        job = running.get(k)
        if not avail[k]:
            state = "unavailable"
        elif job is not None:
            state = "running"
        elif not all(resolved.get(d) == "up_to_date" for d in sd.deps):
            state = "blocked"
        elif pending[k] > 0:
            lj = last.get(k)
            state = "error" if (lj and lj["status"] == "error") else "queued"
        else:
            state = "up_to_date"
        resolved[k] = state
        # The dep still short of done, so a blocked stage can name what it waits on.
        blocker = next((d for d in sd.deps if resolved.get(d) != "up_to_date"), None)
        out.append(
            {
                "kind": k,
                "card": sd.card,
                "counted": sd.counted,
                "state": state,
                "pending": pending[k],
                "progress": job_progress(job),
                "stopped_progress": _stopped_progress(last.get(k)),
                "blocker": blocker,
                "error": (last.get(k) or {}).get("message") if state == "error" else None,
            }
        )
    return out


def job_progress(job: dict | None) -> dict | None:
    """The bar-shaped subset of a job's public payload, or None for no job.

    One shape for every stage, and for the non-stage jobs ``status.py`` reports
    alongside them, so the GUI has a single thing to render.
    """
    if not job:
        return None
    return {
        "percent": job.get("percent"),
        "done": job.get("done", 0),
        "total": job.get("total", 0),
        "current": job.get("current", ""),
        "elapsed": job.get("elapsed", 0),
        "phase": job.get("phase", "working"),
    }


def _stopped_progress(job: dict | None) -> dict | None:
    """Where a job stopped mid-run had got to, for ``_apply_pause`` to show.

    Only a *cancelled* job qualifies: that is the pause path, and its committed
    batches are what the next run resumes from. A finished run has nothing to
    resume, an errored one is reported as an error, and one still preparing
    never reached a count worth drawing.
    """
    if not job or job.get("status") != "cancelled":
        return None
    p = job_progress(job)
    return p if p and p["phase"] == "working" and (p["done"] or p["total"]) else None


# Precedence when several stages share one card (scan + enrich): the most
# "active" state wins, so a running enrich keeps the Scan card spinning.
_STATE_RANK = {
    "running": 5,
    "error": 4,
    "queued": 3,
    "blocked": 2,
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
    """
    by_kind = {m["kind"]: m for m in members}
    if by_kind.get(SCAN, {}).get("state") == "running":
        return by_kind[SCAN]["progress"], message
    if by_kind.get(ENRICH, {}).get("state") == "running":
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
        return f"Fingerprinting {done:,} of {total:,} photos…"
    return message


def _preparing_message(progress: dict) -> str:
    """What a stage says between starting and reaching its first file: whatever
    its setup last reported (see ``JobContext.preparing``), or nothing.

    The reports are written for a log line and several trail an ellipsis of
    their own ("downloading face models (buffalo_l) …"), which behind this
    prefix would be the second one in six words. One is enough.
    """
    detail = (progress.get("current") or "").strip().rstrip("…. ")
    return f"Preparing… · {detail}" if detail else "Preparing…"


def _card(card_id: str, members: list[dict], blocker_card: dict[str, str | None]) -> dict:
    """Roll one card's member stages into the dict the GUI renders."""
    lead = max(members, key=lambda s: _STATE_RANK[s["state"]])
    state = lead["state"]
    counted = any(m["counted"] for m in members)
    pending = sum(m["pending"] for m in members if m["counted"]) if counted else None
    progress = next((m["progress"] for m in members if m["progress"]), None)
    blocker = lead["blocker"]
    blocker_card[card_id] = _CARD_OF.get(blocker) if blocker else None
    message = _message(card_id, state, pending, blocker, lead.get("error"))

    if card_id == "scan":
        progress, message = _scan_card_progress(members, progress, message)
    elif card_id == "dedup" and state == "running" and progress:
        message = _dedup_card_message(progress, message)
    # Last, so it outranks the wording above: "Finding duplicates…" and
    # "Fingerprinting 0 of 40,000 photos…" are both claims about a loop that
    # has not started yet.
    if progress and progress.get("phase") == "preparing":
        message = _preparing_message(progress)
        progress = None

    bc_id = blocker_card[card_id]
    return {
        "id": card_id,
        "label": CARD_LABEL[card_id],
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
        "waiting_on": CARD_LABEL.get(bc_id) if bc_id else None,
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
    for c in result:
        bc = blocker_card.get(c["id"])
        if c["state"] == "blocked" and bc is not None and bc in running_cards:
            c["next"] = True
            c["message"] = f"Up next · after {CARD_LABEL[bc]}"


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


def cards(states: list[dict], paused_stages: frozenset[str] | set[str] = frozenset()) -> list[dict]:
    """Roll per-stage states up into the display cards the GUI renders verbatim.

    ``paused_stages`` holds the card ids the user paused individually. A card
    carries two flags for it: ``paused`` (this card's own toggle, what its
    button reflects) and ``stalled`` (it cannot progress — either it is paused
    or everything it waits on is). Only ``stalled`` may be used to decide that
    no work is coming, since a card blocked behind a paused stage is just as
    stopped as the paused one itself.
    """
    by_card: dict[str, list[dict]] = {}
    for s in states:
        by_card.setdefault(s["card"], []).append(s)

    blocker_card: dict[str, str | None] = {}
    result = [
        _card(card_id, by_card[card_id], blocker_card)
        for card_id in CARD_ORDER
        if by_card.get(card_id)
    ]
    _mark_up_next(result, blocker_card)
    _mark_stalled(result, blocker_card, paused_stages)
    return result


_CARD_OF = {sd.kind: sd.card for sd in STAGES}


def card_of(kind: str) -> str:
    """The display card a stage kind rolls up into (its own name if it has none).

    Per-stage pause is expressed in card ids, because cards are what the user
    sees and clicks; the scheduler works in kinds, so it resolves through here
    (pausing "scan" stops the scan ∥ enrich pair the one card represents).
    """
    return _CARD_OF.get(kind, kind)


def kinds_of(card: str) -> frozenset[str]:
    """Every stage kind that rolls up into one display card."""
    return frozenset(k for k, c in _CARD_OF.items() if c == card)


def is_stage_kind(kind: str) -> bool:
    """Whether this job kind is a pipeline stage at all.

    False for the one-off jobs a user action kicks (a re-cluster after an
    unmerge), which have no card of their own -- ``status._extra_jobs`` is
    what reports those.
    """
    return kind in _CARD_OF


def _message(
    card_id: str, state: str, pending: int | None, blocker: str | None, error: str | None
) -> str | None:
    """Fixed wording the client shows for every non-terminal state. The
    up_to_date ("done") message is left to the client, which already holds the
    per-domain summary numbers (duplicate count, faces found, …)."""
    if state == "running":
        return _RUN_TEXT.get(card_id, "Working now")
    if state == "blocked":
        blocker_card = _CARD_OF.get(blocker) if blocker else None
        waiting = CARD_LABEL.get(blocker_card, "earlier steps") if blocker_card else "earlier steps"
        return f"Waiting for {waiting}…"
    if state == "queued":
        if pending and pending > 0:
            noun = "item" if pending == 1 else "items"
            return f"{pending:,} {noun} queued"
        return "Queued"
    if state == "unavailable":
        return _UNAVAILABLE_TEXT.get(card_id, "Not available")
    if state == "error":
        return error or "Last run failed, will retry"
    return None
