# 0005. Pipeline status is in-memory, not persisted

- **Status:** Accepted
- **Date:** 2026-08-01

## Context

While an archive is open, the GUI runs its stages (scan, metadata, dedup,
people & pets, places, semantic indexing) automatically and shows their
progress on a status panel that polls the backend roughly once a second. That
status has to be a single, unambiguous source of truth: two representations
of "what is scan doing right now" that can disagree is worse than one.

## Decision

There is exactly one in-memory source of truth for pipeline status —
`organize_archive.pipeline.manager.JobManager`'s job registry (`_jobs`) — and
one event-driven scheduler thread, `organize_archive.pipeline.scheduler.Scheduler`,
that decides what to start next and never performs work itself. Status is
deliberately **not** written to the database. The route that serves it,
`snapshot` in `organize_archive/web/routes/pipeline.py`, says so directly in
its own comment: "Single source of truth for pipeline status: the same
resolved stage list the scheduler acts on, so cards never disagree with
what's actually running." It calls `stages.snapshot(...)`, which builds the
response from the live `JobManager` (`req.jobs`) rather than from any
persisted table.

The reasoning, read from the code: the pipeline runs to completion once and
then goes idle — it is not a long-running service whose state needs to
survive independently of the process. `manager.py`'s own threading-contract
docstring frames the whole module around that fact: three kinds of thread
touch a `JobManager` (HTTP threads, the one scheduler thread, and per-job
worker threads), and only the scheduler thread may start a job. Persisting
status would create a second copy of a truth that only matters while the
process is alive, and — the sharper problem — a stale row surviving a crash
or an unclean shutdown would misreport a stage as "running" when nothing is
actually running, which `manager.py`'s shutdown logic goes out of its way to
avoid: `shutdown()` cancels every job and waits up to a timeout for workers to
actually stop, logging which kinds were still running if the timeout is hit,
rather than leaving anything for a persisted status row to describe
incorrectly later.

**What *is* persisted is user intent, not status.** Two flags survive a
restart, and both live in `Config` (`config.json`), not the database:

- `Config.pipeline_paused` — the whole-pipeline pause, a single flag because
  only one archive is ever open in the GUI at a time
  (`JobManager.__init__` seeds `self._paused` from it, and `set_paused`
  writes it back through `self.cfg.save()`).
- `Config.paused_stages` — the per-stage pause, keyed by the display card id
  (`"scan"`, `"dedup"`, `"detect"`, `"places"`, `"semantic"` — see
  `pipeline/stages.py`'s `CARD_ORDER`), independent of the whole-pipeline
  flag and only meaningful while it is off (`set_stage_paused`).

In both setters, the in-memory flag is updated first and treated as
authoritative even if the subsequent `cfg.save()` fails (caught and logged,
not raised) — the comments on both methods make the same point: a disk
hiccup must not silently leave a user who just asked to stop the CPU load
still running, and the cost of that write failing is only that the pause is
forgotten on the next restart, not that it fails to take effect now.

## Consequences

- A restart has nothing to reconcile: the scheduler recomputes what is
  outstanding straight from the catalogue (pending counts per stage), so
  there is no persisted "was running" state that could ever disagree with
  reality.
- The whole-pipeline and per-stage pauses are the one thing that does need
  to survive a restart — they are decisions the user made, not facts about
  a process that is now gone — and they live in `config.json` for exactly
  that reason.
- A crash mid-stage loses nothing beyond the last committed batch (every
  long stage commits incrementally; see `manager.py`'s cancellation
  contract), and produces no stale "running" status for the next process to
  clean up.
