"""Single source of truth for pipeline stage status.

Both the scheduler (deciding what to start next) and the GUI (deciding what to
render) read the *same* resolved stage list from here, so a status card can
never disagree with what the pipeline is actually doing.

A stage's state is derived, in one place, from two things:

* **countable pending work in the catalog** — a file with no ``dates`` row *is*
  enrich-pending; a canonical image with no ``face_scan`` row *is* faces-pending.
  Nothing about "what's left to do" is persisted separately, because the catalog
  already implies it and re-derives it for free on restart.
* **the live in-process jobs** — whether a worker of that kind is running right
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
SCAN, ENRICH, DEDUP, PLACES, PETS, FACES, SEMANTIC = (
    "scan", "enrich", "dedup", "places", "pets", "faces", "semantic")

# Stages that take the single DB-writer lock run one at a time; the rest use
# their own connection and overlap freely (scan ∥ enrich ∥ semantic).
LOCK_KINDS = frozenset({DEDUP, PLACES, PETS, FACES})
PARALLEL_KINDS = frozenset({SCAN, ENRICH, SEMANTIC})


@dataclass(frozen=True)
class StageDef:
    kind: str
    deps: tuple[str, ...]        # upstream kinds that must be up_to_date first
    card: str                    # which display card this stage rolls up into
    counted: bool                # whether `pending` is a number worth showing


# Order matters: dependency resolution and the scheduler both walk this list in
# order, so a stage's deps always appear before it.
STAGES: tuple[StageDef, ...] = (
    StageDef(SCAN,     (),               "scan",     True),
    StageDef(ENRICH,   (),               "scan",     True),   # runs parallel to scan
    StageDef(DEDUP,    (SCAN, ENRICH),   "dedup",    False),  # wholesale rebuild, no count
    StageDef(PLACES,   (DEDUP,),         "places",   True),
    StageDef(PETS,     (DEDUP,),         "pets",     True),
    StageDef(FACES,    (PETS,),          "faces",    True),   # pet boxes suppress face FPs
    StageDef(SEMANTIC, (DEDUP,),         "semantic", True),
)

# Display cards, in the order the Overview renders them.
CARD_ORDER = ("scan", "dedup", "pets", "faces", "places", "semantic")
CARD_LABEL = {
    "scan": "Scan & metadata", "dedup": "Duplicates", "pets": "Pets",
    "faces": "People", "places": "Places", "semantic": "Semantic Search",
}
# What a card says while its stage is actively running.
_RUN_TEXT = {
    "scan": "Scanning and extracting metadata", "dedup": "Comparing media now",
    "pets": "Detecting pets now", "faces": "Detecting faces now",
    "places": "Clustering locations now", "semantic": "Indexing media now",
}
_UNAVAILABLE_TEXT = {
    "faces": "Face detection unavailable", "pets": "Pet detection unavailable",
    "semantic": "Not configured",
}


def _availability(cfg: Config) -> dict[str, bool]:
    from ..faces import backend as fb
    from ..pets import backend as pb
    from . import semantic
    return {
        SCAN: True, ENRICH: True, DEDUP: True, PLACES: True,
        PETS: pb.available(), FACES: fb.available(),
        SEMANTIC: semantic.api_key_available(),
    }


def _pending(cfg: Config, jobs, root_id: int, root_path: str,
             avail: dict[str, bool]) -> dict[str, int]:
    """Countable backlog per stage, from the catalog. One connection for the
    cheap DB counts; the expensive disk walk is served from the manager's cache."""
    from . import queries, semantic
    from ..pets.extract import scan_source as pet_scan_source

    conn = db.open_readonly(cfg.db_path)
    try:
        indexed = conn.execute(
            "SELECT COUNT(*) FROM files f WHERE f.present=1 AND f.root_id=?",
            (root_id,)).fetchone()[0]
        enriched = conn.execute(
            """SELECT COUNT(*) FROM files f JOIN dates d ON d.file_id=f.id
               WHERE f.present=1 AND f.root_id=?""", (root_id,)).fetchone()[0]
        # Geotagged files not yet attached to any place (covers the first-time
        # bootstrap too: with no clusters yet, every geotagged file is unplaced).
        geo_unplaced = conn.execute(
            """SELECT COUNT(*) FROM files f JOIN geo g ON g.file_id=f.id
               WHERE f.present=1 AND f.root_id=? AND g.lat IS NOT NULL
                 AND f.id NOT IN (SELECT file_id FROM place_cluster_members)""",
            (root_id,)).fetchone()[0]
    finally:
        conn.close()

    on_disk = jobs.disk_count(root_id, root_path)
    new_files = max(0, on_disk - indexed) if on_disk is not None else 0

    return {
        SCAN: new_files,
        ENRICH: max(0, indexed - enriched),
        # Dedup rebuilds wholesale, so it has no per-file backlog — a dirty flag,
        # set when scan/enrich change data and cleared on a successful rebuild.
        DEDUP: 1 if jobs.dedup_needed(root_id) else 0,
        PLACES: geo_unplaced,
        PETS: (queries.pets_pending(cfg.db_path, root_id, pet_scan_source(cfg))
               if avail[PETS] else 0),
        FACES: queries.faces_pending(cfg.db_path, root_id) if avail[FACES] else 0,
        SEMANTIC: (queries.semantic_pending(cfg.db_path, root_id)
                   if avail[SEMANTIC] else 0),
    }


def stage_states(cfg: Config, jobs, root_id: int, root_path: str) -> list[dict]:
    """Resolve every stage to one state, used by BOTH the scheduler and the API.

    Returns one dict per stage (not per card) so the scheduler can act on the
    fine-grained scan/enrich split; ``cards()`` rolls them up for display.
    """
    avail = _availability(cfg)
    pending = _pending(cfg, jobs, root_id, root_path, avail)

    running: dict[str, dict] = {}
    last: dict[str, dict] = {}
    for j in jobs.list(root_id):          # newest first
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
        out.append({
            "kind": k, "card": sd.card, "counted": sd.counted,
            "state": state, "pending": pending[k],
            "progress": _progress(job), "blocker": blocker,
            "error": (last.get(k) or {}).get("message") if state == "error" else None,
        })
    return out


def _progress(job: dict | None) -> dict | None:
    if not job:
        return None
    return {"percent": job.get("percent"), "done": job.get("done", 0),
            "total": job.get("total", 0), "current": job.get("current", ""),
            "elapsed": job.get("elapsed", 0)}


# Precedence when several stages share one card (scan + enrich): the most
# "active" state wins, so a running enrich keeps the Scan card spinning.
_STATE_RANK = {"running": 5, "error": 4, "queued": 3, "blocked": 2,
               "unavailable": 1, "up_to_date": 0}


def cards(states: list[dict]) -> list[dict]:
    """Roll per-stage states up into the display cards the GUI renders verbatim."""
    by_card: dict[str, list[dict]] = {}
    for s in states:
        by_card.setdefault(s["card"], []).append(s)

    result = []
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
        result.append({
            "id": card_id, "label": CARD_LABEL[card_id], "state": state,
            "pending": pending, "counted": counted, "progress": progress,
            "waiting_on": CARD_LABEL.get(_CARD_OF.get(blocker)) if blocker else None,
            "message": _message(card_id, state, pending, blocker, lead.get("error")),
        })
    return result


_CARD_OF = {sd.kind: sd.card for sd in STAGES}


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
        return error or "Last run failed — will retry"
    return None


def snapshot(cfg: Config, jobs, root_id: int, root_path: str) -> dict:
    """The `/api/pipeline` payload: resolved cards plus one overall verdict."""
    states = stage_states(cfg, jobs, root_id, root_path)
    card_list = cards(states)
    if any(c["state"] == "running" for c in card_list):
        overall = "running"
    elif any(c["state"] in ("queued", "blocked", "error") for c in card_list):
        overall = "working"
    else:
        overall = "idle"
    return {"root_id": root_id, "overall": overall, "stages": card_list}
