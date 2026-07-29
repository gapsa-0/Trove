"""Single source of truth for pipeline stage status.

Both the scheduler (deciding what to start next) and the GUI (deciding what to
render) read the *same* resolved stage list from here, so a status card can
never disagree with what the pipeline is actually doing.

A stage's state is derived, in one place, from two things:

* **countable pending work in the catalog**, a file with no ``dates`` row *is*
  enrich-pending; a canonical image with no ``face_scan`` row *is* faces-pending.
  Nothing about "what's left to do" is persisted separately, because the catalog
  already implies it and re-derives it for free on restart.
* **the live in-process jobs**, whether a worker of that kind is running right
  now, and whether the last one errored.

Every stage resolves through the SAME rule
(``unavailable → error → running → blocked → queued → up_to_date``), which is why
the six cards finally behave identically instead of each computing its own truth.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Config
from ..db import database as db

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
    from . import semantic

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
    cfg: Config, jobs, root_id: int, root_path: str, avail: dict[str, bool], allow_walk: bool
) -> dict[str, int]:
    """Countable backlog per stage, from the catalog. One connection for the
    cheap DB counts; the expensive disk walk is served from the manager's cache."""
    from . import queries, semantic
    from ..pets.extract import scan_source as pet_scan_source

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
    if settled or on_disk is None:
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
            queries.detect_pending(db_path, root_id, pet_scan_source(cfg), cfg.detect_video_frames)
            if avail[DETECT]
            else 0
        ),
        SEMANTIC: (queries.semantic_pending(db_path, root_id) if avail[SEMANTIC] else 0),
    }


def stage_states(
    cfg: Config, jobs, root_id: int, root_path: str, allow_walk: bool = False
) -> list[dict]:
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
                "progress": _progress(job),
                "blocker": blocker,
                "error": (last.get(k) or {}).get("message") if state == "error" else None,
            }
        )
    return out


def _progress(job: dict | None) -> dict | None:
    if not job:
        return None
    return {
        "percent": job.get("percent"),
        "done": job.get("done", 0),
        "total": job.get("total", 0),
        "current": job.get("current", ""),
        "elapsed": job.get("elapsed", 0),
    }


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

    result = []
    blocker_card: dict[str, str | None] = {}
    for card_id in CARD_ORDER:
        members = by_card.get(card_id, [])
        if not members:
            continue
        lead = max(members, key=lambda s: _STATE_RANK[s["state"]])
        state = lead["state"]
        counted = any(m["counted"] for m in members)
        pending = sum(m["pending"] for m in members if m["counted"]) if counted else None
        progress = next((m["progress"] for m in members if m["progress"]), None)
        blocker = lead["blocker"]
        blocker_card[card_id] = _CARD_OF.get(blocker) if blocker else None
        message = _message(card_id, state, pending, blocker, lead.get("error"))

        # The Scan card is the only one that fuses two stages running in parallel
        # (scan ∥ enrich), which count different things. Never share one bar across
        # both: reusing it makes the fill shoot past 100% and then rewind when the
        # source flips. Instead show the scan bar *only while scanning*; once the
        # on-disk walk is done but metadata extraction is still catching up, drop
        # the bar and say so plainly.
        if card_id == "scan":
            by_kind = {m["kind"]: m for m in members}
            if by_kind.get(SCAN, {}).get("state") == "running":
                progress = by_kind[SCAN]["progress"]
            elif by_kind.get(ENRICH, {}).get("state") == "running":
                progress = None
                message = "Finalizing metadata extraction…"

        # Dedup is `counted=False` (a wholesale rebuild has no per-file backlog),
        # so it would otherwise sit on the flat "Finding duplicates…" text for
        # the whole run. It actually has two real phases sharing one job's
        # progress: `_perceptual_hashes` fingerprints images (current=rel_path),
        # then `run()`'s grouping loop unions them into groups (current=
        # "<n>× exact/perceptual"). Tell them apart from that shape rather than
        # threading a phase flag through jobs.py. `done > total` catches the
        # instant the grouping phase starts, before its own first progress
        # update: `total` has already flipped from the image count to the
        # (usually smaller) group count while `current`/`done` still hold the
        # fingerprinting pass's final values.
        if card_id == "dedup" and state == "running" and progress:
            current = progress.get("current") or ""
            total = progress.get("total") or 0
            done = progress.get("done") or 0
            if "×" in current or (total and done > total):
                message = "Grouping duplicates…"
            elif total and current and done < total:
                # `current` is only a photo path while fingerprinting is actually
                # running. Without it (no imagehash installed, so `run()` skips
                # straight to grouping and sets `total` itself) claiming to
                # fingerprint would be a lie; the flat text stands instead.
                message = f"Fingerprinting {done:,} of {total:,} photos…"

        result.append(
            {
                "id": card_id,
                "label": CARD_LABEL[card_id],
                "state": state,
                "pending": pending,
                "counted": counted,
                "progress": progress,
                "next": False,
                "waiting_on": CARD_LABEL.get(_CARD_OF.get(blocker)) if blocker else None,
                "message": message,
            }
        )

    # Second pass, the "up next" marker. A blocked card is *next in line* when the
    # stage it's waiting on is running right now: it's the one that starts the
    # moment the current work finishes. Flag it (the GUI colours it and swaps the
    # flat "Waiting for …" line for a "goes next" line) so the queue reads as an
    # ordered pipeline, not a wall of identical "waiting" cards.
    running_cards = {c["id"] for c in result if c["state"] == "running"}
    for c in result:
        bc = blocker_card.get(c["id"])
        if c["state"] == "blocked" and bc in running_cards:
            c["next"] = True
            c["message"] = f"Up next · after {CARD_LABEL[bc]}"

    # Third pass, the pause flags. CARD_ORDER is dependency-ordered, so a card's
    # blocker has already been resolved by the time we reach it and one forward
    # walk is enough to propagate "stalled" down a chain.
    stalled: dict[str, bool] = {}
    for c in result:
        bc = blocker_card.get(c["id"])
        c["paused"] = c["id"] in paused_stages
        c["stalled"] = c["paused"] or bool(bc and stalled.get(bc))
        stalled[c["id"]] = c["stalled"]
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


def _message(card_id: str, state: str, pending, blocker, error) -> str | None:
    """Fixed wording the client shows for every non-terminal state. The
    up_to_date ("done") message is left to the client, which already holds the
    per-domain summary numbers (duplicate count, faces found, …)."""
    if state == "running":
        return _RUN_TEXT.get(card_id, "Working now")
    if state == "blocked":
        waiting = CARD_LABEL.get(_CARD_OF.get(blocker), "earlier steps")
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


# Jobs the scheduler never queues on its own: a user action kicks them (a
# non-human review correction, undoing a merge). They aren't STAGES -- they have
# no backlog to count and no place in the dependency order -- but they DO take
# the single writer lock, so a user watching an unexplained pause deserves to
# see them. Reported separately from the cards so the Overview's stage grid
# keeps its fixed five-card shape.
_EXTRA_JOB_LABEL = {
    "face_cluster": "Updating people",
    "pet_cluster": "Updating pets",
}


def _extra_jobs(jobs, root_id: int) -> list[dict]:
    """Running jobs that belong to no stage card, in the cards' output shape."""
    out, seen = [], set()
    for j in jobs.list(root_id):
        kind = j["kind"]
        if j["status"] != "running" or kind in _CARD_OF or kind in seen:
            continue
        seen.add(kind)
        out.append(
            {
                "id": kind,
                "label": _EXTRA_JOB_LABEL.get(kind, kind),
                "state": "running",
                "pending": None,
                "counted": False,
                "progress": _progress(j),
                "next": False,
                "waiting_on": None,
                "message": f"{_EXTRA_JOB_LABEL.get(kind, kind)}…",
                "paused": False,
                "stalled": False,
            }
        )
    return out


def snapshot(cfg: Config, jobs, root_id: int, root_path: str) -> dict:
    """The `/api/pipeline` payload: resolved cards plus one overall verdict.

    ``paused`` reflects the whole-pipeline pause (JobManager.paused()). While
    paused, a queued/error card must not claim work is imminent, so its
    message is overridden to "Paused" (its `state` is left untouched -- the
    client and scheduler both key off that); and ``overall`` becomes "paused"
    once nothing is actually still running.

    Individually paused stages (``jobs.paused_stages()``) work the same way one
    card at a time: their message becomes "Paused", and a card stuck behind one
    says so too instead of promising work that can never start.

    ``extra`` carries running non-stage jobs (see _extra_jobs). They count
    toward ``overall`` -- reporting "idle" while a recluster holds the writer
    would be a lie -- but stay out of ``stages`` so the Overview grid is
    unaffected; only the ambient sidebar chip reads them.
    """
    states = stage_states(cfg, jobs, root_id, root_path)
    paused = bool(jobs.paused()) if hasattr(jobs, "paused") else False
    per_stage = frozenset(jobs.paused_stages()) if hasattr(jobs, "paused_stages") else frozenset()
    card_list = cards(states, per_stage)
    extra = _extra_jobs(jobs, root_id)
    for c in card_list:
        if c["state"] not in ("queued", "blocked", "error"):
            continue
        if paused or c["paused"]:
            c["next"] = False
            c["message"] = "Paused"
        elif c["stalled"]:
            c["next"] = False
            c["message"] = f"Waiting for {c['waiting_on'] or 'earlier steps'} (paused)"
    if extra or any(c["state"] == "running" for c in card_list):
        overall = "running"
    elif paused:
        overall = "paused"
    elif any(c["state"] in ("queued", "blocked", "error") and not c["stalled"] for c in card_list):
        overall = "working"
    elif any(c["state"] in ("queued", "blocked", "error") for c in card_list):
        # Everything still outstanding is stopped by a per-stage pause; the
        # pipeline is not idle, it is waiting on the user.
        overall = "paused"
    else:
        overall = "idle"
    return {
        "root_id": root_id,
        "overall": overall,
        "stages": card_list,
        "extra": extra,
        "paused": paused,
        "paused_stages": sorted(per_stage),
    }
