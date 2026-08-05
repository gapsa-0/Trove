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

``cards.py`` sits on top of this and rolls those states up into what the
Overview draws -- which stage leads a shared card, what it says, which bar it
gets. ``status.py`` sits on top of *that* with the part only the GUI needs --
the pause overlay, the jobs that belong to no card, one overall verdict. Neither
decides a state of its own, so there is nothing in either for the scheduler to
disagree with, and neither is imported from here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .. import features
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

# Display cards, in the order the Overview renders them. Dependency-ordered,
# which _mark_stalled's single forward walk relies on, and the same order the
# setup panel draws its chain in (``tests/unit/test_features.py``).
CARD_ORDER = ("scan", "dedup", "detect", "places", "semantic")
# What each card is *called*, what it says while it runs and which mark it
# carries all come from ``features.py``, composed per card from the features
# the archive actually enabled -- there is deliberately no table of card names
# here. Three of them used to live in this module and two more in the frontend,
# and every one of them named the same five things differently from the setup
# panel that offered them. The roll-up that reads this order lives in
# ``cards.py``; nothing here imports it back.


def _availability(cfg: Config, enabled: tuple[str, ...]) -> dict[str, bool]:
    from ..detect import extract as dx
    from ..services import semantic

    # Both of these ask "are the dependencies importable", never "are the model
    # weights on disk": an unavailable stage is never queued, and a stage still
    # downloads its own weights when it finds them absent, so gating on the
    # files would be a deadlock. The fetch job normally gets there first
    # (``runners/models.py``), but it is a shortcut that may have failed or been
    # cancelled -- nothing here may depend on it having run.
    #
    # Detection is asked about the detectors this archive actually wants: an
    # importable face backend does not make the stage available to an archive
    # that only asked for Pets.
    return {
        SCAN: True,
        ENRICH: True,
        DEDUP: True,
        PLACES: True,
        DETECT: dx.available(features.detectors(enabled)),
        SEMANTIC: semantic.available(),
    }


def _pending(
    cfg: Config,
    jobs: JobManager,
    root_id: int,
    root_path: str,
    avail: dict[str, bool],
    allow_walk: bool,
    enabled: tuple[str, ...],
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
            detect_pending(
                db_path,
                root_id,
                pet_scan_source(cfg),
                cfg.detect_video_frames,
                features.detectors(enabled),
            )
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

    Stages belonging to a feature this archive did not ask for are absent from
    the result entirely -- not reported as some "off" state. That absence is the
    whole gate: the scheduler starts what this list says is queued, so a stage
    that is not here is never started, never downloads its weights, and has no
    card for ``cards()`` to render.
    """
    enabled = cfg.archive_features(root_id)
    wanted = features.stage_kinds(enabled)
    avail = _availability(cfg, enabled)
    pending = _pending(cfg, jobs, root_id, root_path, avail, allow_walk, enabled)

    running: dict[str, dict] = {}
    last: dict[str, dict] = {}
    for j in jobs.list(root_id):  # newest first
        last.setdefault(j["kind"], j)
        if j["status"] == "running":
            running.setdefault(j["kind"], j)

    resolved: dict[str, str] = {}
    out: list[dict] = []
    for sd in (sd for sd in STAGES if sd.kind in wanted):
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
        "recheck_below": job.get("recheck_below", 0),
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
