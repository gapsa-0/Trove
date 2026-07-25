"""In-process background task runner for the GUI.

Runs long operations (scan, enrich) in worker threads and exposes their live
progress for polling. Write-tasks are serialized by a global lock so only one
touches the SQLite writer at a time (reads via the GUI stay concurrent).
"""

from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass, field, asdict

from ..config import Config
from ..db import database as db
from ..scan import walker


@dataclass
class Job:
    id: int
    kind: str                 # "scan" | "enrich" | "dedup" | "places" | "faces" | "pets" | "semantic"
    root_id: int | None
    root_path: str | None
    force: bool = False
    status: str = "running"   # running | done | error | cancelled
    total: int = 0
    done: int = 0
    current: str = ""
    message: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def public(self) -> dict:
        d = asdict(self)
        d["percent"] = round(100 * self.done / self.total, 1) if self.total else None
        d["elapsed"] = round((self.finished_at or time.time()) - self.started_at, 1)
        return d


class _JobProgress:
    """Adapter with the interface walker/enrich expect (.total, .update()).

    ``base`` / ``fixed_total`` let a multi-pass job (faces: detect in chunks,
    re-clustering between them) present one continuous bar: each pass reports
    0..chunk offset by ``base``, while the grand ``total`` stays put instead of
    being reset to the chunk size on every pass."""
    def __init__(self, job: Job, cancel: threading.Event, base: int = 0,
                 fixed_total: bool = False):
        self.job = job
        self._cancel = cancel
        self.base = base
        self._fixed_total = fixed_total

    @property
    def total(self):
        return self.job.total

    @total.setter
    def total(self, v):
        if not self._fixed_total:
            self.job.total = v or 0

    def update(self, done, _bytes=0, current=""):
        if self._cancel.is_set():
            raise KeyboardInterrupt
        self.job.done = self.base + done
        if current:
            self.job.current = current

    def close(self):
        pass


class JobManager:
    # Idle poll interval backs off (up to _AUTO_MAX) when a tick finds nothing
    # to do, so a quiet archive doesn't get walked every few seconds forever.
    _AUTO_MIN = 10
    _AUTO_MAX = 300
    # Images per detect-then-recluster chunk in the detect job (see _run_detect):
    # small enough that people/pets appear early in a multi-hour run, large enough
    # that repeated clustering stays a small fraction of total time. Detection is
    # heavier per image than one detector was (SCRFD + YOLOX + embeds), so the
    # chunk is smaller than the old faces chunk.
    _DETECT_CHUNK = 600
    # Freshness disk walk is reused for this long before re-walking (see
    # _walk_cache). Comfortably below the idle backoff so an idle tick still
    # re-walks and notices files added to the source folder.
    _WALK_TTL = 60.0
    # After a job of some kind errors, wait this long before auto-restarting the
    # same kind, so a persistent failure backs off instead of spinning.
    _ERROR_COOLDOWN = 120.0

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._jobs: dict[int, Job] = {}
        self._cancels: dict[int, threading.Event] = {}
        self._seq = 0
        self._lock = threading.Lock()          # guards registry
        self._write_lock = threading.Lock()    # serializes DB writers

        # Dedup rebuilds every group wholesale (no per-file backlog), so its
        # "needs a run" signal is this flag: set when scan/enrich change data,
        # cleared only on a *successful* rebuild (so a cancelled/errored dedup
        # still retries). Places, by contrast, has a real count (unplaced
        # geotagged files), so it no longer needs a flag. Defaults dirty so the
        # first open of an archive reconciles duplicates once.
        self._dedup_dirty = True
        self._dedup_root: int | None = None
        # Disk-walk cache per root: counting files on disk is the one expensive
        # part of freshness, so the status endpoint (polled ~1/s) reads a cached
        # count instead of walking ~150k files every time. (monotonic_ts, count)
        self._walk_cache: dict[int, tuple[float, int]] = {}
        # When a stage's job errors, hold off auto-restarting that kind for a
        # cooldown so a hard failure can't hot-loop through the nudge path.
        self._error_at: dict[tuple[int, str], float] = {}
        # Work is deliberately opt-in per visible archive.  Starting the GUI
        # alone must not start touching an archive in the background.
        self._open_root_id: int | None = None
        self._auto_interval = self._AUTO_MIN
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._scheduler = threading.Thread(target=self._auto_loop, daemon=True)
        self._scheduler.start()

    def shutdown(self, timeout: float = 8.0) -> bool:
        """Cancel all work and stop the scheduler before the HTTP server exits."""
        self._stopping.set()
        self._wake.set()
        with self._lock:
            for cancel in self._cancels.values():
                cancel.set()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                active = any(job.status == "running" for job in self._jobs.values())
            if not active:
                self._scheduler.join(timeout=max(0, deadline - time.monotonic()))
                return True
            time.sleep(0.05)
        return False

    # -- introspection ----------------------------------------------------
    def list(self, root_id: int | None = None) -> list[dict]:
        with self._lock:
            js = sorted(self._jobs.values(), key=lambda j: j.id, reverse=True)
        return [j.public() for j in js
                if root_id is None or j.root_id == root_id]

    def active_kind(self, kind: str) -> bool:
        return any(j.status == "running" and j.kind == kind
                   for j in self._jobs.values())

    def disk_count(self, root_id: int, root_path: str,
                   max_age: float | None = None) -> int | None:
        """Files on disk under this root, cached so the polled status endpoint
        never triggers a fresh ~150k-file walk. Returns None if the folder is
        gone."""
        from pathlib import Path
        from ..scan.walker import count_files
        ttl = self._WALK_TTL if max_age is None else max_age
        hit = self._walk_cache.get(root_id)
        now = time.monotonic()
        if hit and now - hit[0] < ttl:
            return hit[1]
        if not Path(root_path).is_dir():
            self._walk_cache.pop(root_id, None)
            return None
        count = count_files(Path(root_path))
        self._walk_cache[root_id] = (now, count)
        return count

    def dedup_needed(self, root_id: int) -> bool:
        """Whether a duplicate rebuild is outstanding for this root."""
        return self._dedup_dirty and self._dedup_root in (None, root_id)

    def current_root_id(self) -> int | None:
        """Which single archive is open in the GUI right now, if any.

        Each archive is a separate database, so content routes that carry no
        root/archive id of their own (thumbnails, the original file) resolve
        against whichever one this is — the GUI only ever browses one at a time.
        """
        return self._open_root_id

    def _error_ready(self, root_id: int, kind: str) -> bool:
        """True once a kind's post-error cooldown has elapsed (or none is set)."""
        at = self._error_at.get((root_id, kind))
        return at is None or (time.monotonic() - at) >= self._ERROR_COOLDOWN

    def stop_archive(self, root_id: int, timeout: float = 10.0) -> bool:
        """Cancel this archive's work and wait briefly for safe DB quiescence.

        Deleting rows while a scanner or metadata worker is still committing
        would let it recreate part of an archive after removal. Jobs observe
        cancellation at their normal batch checkpoints, so this usually returns
        immediately; callers can retry if a long external operation is winding
        down.
        """
        with self._lock:
            # This can include a job that was already being cancelled after the
            # user switched archives. It still needs an explicit signal here:
            # removal must never proceed until every worker for this root exits.
            for jid, job in self._jobs.items():
                if job.status == "running" and job.root_id == root_id:
                    self._cancels[jid].set()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                active = any(j.status == "running" and j.root_id == root_id
                             for j in self._jobs.values())
            if not active:
                return True
            time.sleep(0.05)
        with self._lock:
            return not any(j.status == "running" and j.root_id == root_id
                           for j in self._jobs.values())

    # -- control ----------------------------------------------------------
    def start(self, kind: str, root_id: int | None = None,
              root_path: str | None = None, force: bool = False) -> dict:
        if self._stopping.is_set():
            return {"error": "application is shutting down"}
        # All GUI jobs belong to the archive currently on screen.  This also
        # closes the small race where the user switches archives between a
        # scheduler decision and this call.
        if root_id is not None and root_id != self._open_root_id:
            return {"error": "archive is no longer open"}
        if self.active_kind(kind):
            return {"error": f"a {kind} job is already running"}
        with self._lock:
            self._seq += 1
            job = Job(id=self._seq, kind=kind, root_id=root_id, root_path=root_path,
                      force=force)
            self._jobs[job.id] = job
            cancel = threading.Event()
            self._cancels[job.id] = cancel
        t = threading.Thread(target=self._run, args=(job, cancel), daemon=True)
        t.start()
        return job.public()

    # -- automatic scheduling ----------------------------------------------
    # While an archive is open, this daemon notices pending work and runs the
    # pipeline (scan → enrich → dedup → pets → faces) for it. Closing or switching the
    # archive requests cancellation; long jobs commit in batches and resume the
    # next time that archive is opened.
    def open_archive(self, root_id: int):
        """Allow automatic work for this archive while it is being viewed."""
        # Bring the schema up to date before anything can read: the read-only
        # query paths (thumbnails, the viewer) join tables this version expects,
        # and an archive catalogued by an older one would not have them yet.
        conn = db.connect(self.cfg.archive_db_path(root_id))
        try:
            db.init_db(conn)
        finally:
            conn.close()
        with self._lock:
            previous = self._open_root_id
            self._open_root_id = root_id
            # Changing archives means the old archive is no longer open.  Its
            # resumable job should yield at its next progress checkpoint.
            if previous is not None and previous != root_id:
                for jid, job in self._jobs.items():
                    if job.status == "running" and job.root_id == previous:
                        self._cancels[jid].set()
        self.nudge()

    def close_archive(self, root_id: int | None = None):
        """Stop work when the currently viewed archive is closed."""
        with self._lock:
            if self._open_root_id is None or (
                    root_id is not None and root_id != self._open_root_id):
                return
            closing = self._open_root_id
            self._open_root_id = None
            for jid, job in self._jobs.items():
                if job.status == "running" and job.root_id == closing:
                    self._cancels[jid].set()

    def nudge(self):
        """Wake the scheduler now after an archive has been opened."""
        self._auto_interval = self._AUTO_MIN
        self._wake.set()

    def _auto_loop(self):
        while not self._stopping.is_set():
            self._wake.wait(self._auto_interval)
            self._wake.clear()
            if self._stopping.is_set():
                break
            try:
                acted = self._auto_tick()
            except Exception:
                traceback.print_exc()
                acted = False
            self._auto_interval = (self._AUTO_MIN if acted
                                    else min(self._auto_interval * 1.5, self._AUTO_MAX))

    def _auto_tick(self) -> bool:
        """One scheduling decision, driven entirely by the same pipeline snapshot
        the GUI renders. Starts every stage that is ready to run and returns True
        while any work is outstanding, so the loop keeps the fast interval until
        the pipeline is fully idle.

        Readiness (deps satisfied, backend available, not already running) is
        already resolved in the snapshot: a stage is ``queued`` exactly when it
        should start now. Parallel kinds (scan ∥ enrich ∥ semantic) start
        together; the DB-writer stages (dedup → places/pets → faces) start one at
        a time, matching the single write lock.
        """
        from . import pipeline, queries
        open_root_id = self._open_root_id
        if open_root_id is None:
            return False
        archive = next((a for a in queries.archives(self.cfg)
                        if a["id"] == open_root_id and a["exists"]), None)
        if not archive:
            return False

        states = pipeline.stage_states(self.cfg, self, open_root_id, archive["path"])
        lock_running = any(s["kind"] in pipeline.LOCK_KINDS and s["state"] == "running"
                           for s in states)
        acted = False
        started_lock = False
        for s in states:
            kind, state = s["kind"], s["state"]
            if state not in ("queued", "error"):
                continue
            if state == "error" and not self._error_ready(open_root_id, kind):
                continue
            if kind in pipeline.PARALLEL_KINDS:
                if self.active_kind(kind):
                    continue
            else:  # single-writer stage: at most one at a time
                if lock_running or started_lock:
                    continue
            # Scanning or enriching may change the file set, so a fresh duplicate
            # rebuild is owed once they finish.
            if kind in (pipeline.SCAN, pipeline.ENRICH):
                self._dedup_dirty, self._dedup_root = True, open_root_id
            # dedup/places operate per-root via root_id and ignore root_path.
            path = None if kind in (pipeline.DEDUP, pipeline.PLACES) else archive["path"]
            if "error" not in self.start(kind, open_root_id, path):
                acted = True
                if kind in pipeline.LOCK_KINDS:
                    started_lock = True
        # Keep polling promptly while anything is running or waiting to run.
        outstanding = any(s["state"] in ("running", "queued", "blocked", "error")
                          for s in states)
        return acted or outstanding

    # -- worker -----------------------------------------------------------
    def _run(self, job: Job, cancel: threading.Event):
        try:
            if job.kind == "semantic":
                self._run_semantic(job, cancel)
            elif job.kind in ("scan", "enrich"):
                # Scan and enrich each use their own connection and bypass the
                # write lock so they can run in parallel. SQLite WAL mode
                # serializes their brief commits safely.
                conn = db.connect(self.cfg.archive_db_path(job.root_id))
                db.init_db(conn)
                try:
                    if job.kind == "scan":
                        self._run_scan(conn, job, cancel)
                    else:
                        self._run_enrich(conn, job, cancel)
                finally:
                    conn.close()
            else:
                with self._write_lock:
                    conn = db.connect(self.cfg.archive_db_path(job.root_id))
                    db.init_db(conn)
                    try:
                        if job.kind == "dedup":
                            self._run_dedup(conn, job, cancel)
                        elif job.kind == "places":
                            self._run_places(conn, job, cancel)
                        elif job.kind == "detect":
                            self._run_detect(conn, job, cancel)
                        elif job.kind == "face_cluster":
                            self._run_face_cluster(conn, job, cancel)
                        else:
                            raise ValueError(f"unknown job kind: {job.kind}")
                    finally:
                        conn.close()
            job.status = "done"
            # A successful rebuild is the only thing that clears the dedup flag,
            # so a cancelled or errored dedup stays queued and retries.
            if job.kind == "dedup":
                self._dedup_dirty = False
            self._error_at.pop((job.root_id, job.kind), None)
        except KeyboardInterrupt:
            job.status = "cancelled"
            job.message = "cancelled; progress saved"
        except Exception as e:
            job.status = "error"
            job.message = f"{e}"
            self._error_at[(job.root_id, job.kind)] = time.monotonic()
            traceback.print_exc()
        finally:
            job.finished_at = time.time()
            # A finished scan changed what's on disk-vs-indexed; drop the cached
            # walk so freshness reflects it immediately.
            if job.kind == "scan":
                self._walk_cache.pop(job.root_id, None)
            # React to completion at once: the next ready stage starts in
            # milliseconds instead of waiting out the idle poll interval.
            self.nudge()

    def _run_scan(self, conn, job: Job, cancel):
        from pathlib import Path

        prog = _JobProgress(job, cancel)
        run_started = db.now_iso()
        # An archive database has exactly one root; job.root_path is always
        # supplied by the scheduler, this is just a defensive fallback.
        roots = [job.root_path] if job.root_path else [self.cfg.archive_path(job.root_id)]
        prog.total = sum(
            walker.count_files(__import__("pathlib").Path(r))
            for r in roots if __import__("pathlib").Path(r).is_dir()
        )
        base = 0
        for r in roots:
            stats = walker.scan_root(conn, self.cfg, r, run_started,
                                     progress=prog, base_done=base,
                                     # Small batches so the parallel enrich job
                                     # can begin reading committed rows quickly.
                                     commit_every=80)
            base += stats.seen
        job.message = f"{base} files scanned"

    def _run_enrich(self, conn, job: Job, cancel):
        from ..metadata import enrich as enrich_mod
        prog = _JobProgress(job, cancel)
        root_ids = (job.root_id,) if job.root_id else None
        stats = enrich_mod.enrich(conn, self.cfg, progress=prog, root_ids=root_ids)
        job.message = (f"{stats.processed} processed, "
                       f"{stats.with_takeout} Takeout-matched, "
                       f"{stats.with_gps} with GPS")

    def _run_dedup(self, conn, job: Job, cancel):
        from ..dedup import exact
        prog = _JobProgress(job, cancel)
        stats = exact.run(conn, self.cfg, progress=prog, root_id=job.root_id)
        # Hidden files are duplicate copies. They must never consume semantic
        # storage or appear as a stale vector if a prior run overlapped dedup.
        conn.execute(
            "DELETE FROM semantic_embeddings WHERE file_id IN "
            "(SELECT id FROM files WHERE hidden=1)"
        )
        conn.commit()
        job.message = (f"{stats.groups} groups, {stats.duplicate_files} duplicates, "
                       f"{stats.reclaimable_bytes/1e9:.1f} GB reclaimable")

    def _run_places(self, conn, job: Job, cancel):
        # Keep map places in sync WITHOUT ever destroying user edits. Places are
        # durable entities: a root is clustered from scratch only the first time
        # (bootstrap); afterwards new geotagged files are attached incrementally
        # (assign_unplaced), so named/pinned places and manual attachments persist.
        # Each archive is now its own database, so this only ever touches the
        # one this job belongs to.
        from ..geo.clusters import cluster_places, assign_unplaced
        job.total, job.done = 1, 0
        has_places = conn.execute(
            "SELECT 1 FROM place_clusters WHERE root_id=? LIMIT 1", (job.root_id,)
        ).fetchone()
        touched = assign_unplaced(conn, job.root_id).points if has_places else 0
        if not has_places:
            cluster_places(conn, job.root_id)
        job.done = 1
        job.message = f"{touched} new geotagged files placed"

    def _run_detect(self, conn, job: Job, cancel):
        # Part of the automatic pipeline (after dedup). ONE decode per image runs
        # both detectors — people via SCRFD, animals via YOLOX — and the animal
        # boxes cross-check the faces inline (a face inside an animal box is
        # dropped from People). Detect in chunks and re-cluster people + pets after
        # each so both views fill in progressively during a long run.
        from ..detect import extract as dx
        from ..faces import cluster as fc
        from ..pets import cluster as pc
        # Progress is cumulative over ALL canonical images, not just this run's
        # backlog: total = every canonical image, done starts at how many are
        # already detected. So the bar/% match the "Detected N / total" tile and
        # survive resuming across restarts (no misleading per-run total).
        total = dx.image_count(conn, job.root_id)
        already = max(0, total - dx.pending_count(conn, self.cfg, job.root_id))
        job.total, job.done = total, already
        # Load both detector model sets once and reuse across every chunk.
        face_be, pet_be = dx.make_backends(
            self.cfg, log=lambda m: setattr(job, "current", m))
        processed = faces_found = animals = suppressed = human_pets = 0
        turned = 0
        while True:
            if cancel.is_set():
                raise KeyboardInterrupt
            prog = _JobProgress(job, cancel, base=already + processed, fixed_total=True)
            st = dx.extract(conn, self.cfg, progress=prog, limit=self._DETECT_CHUNK,
                            face_be=face_be, pet_be=pet_be)
            if st.processed == 0:
                break
            processed += st.processed
            faces_found += st.faces_found
            animals += st.animals
            suppressed += st.nonhuman_suppressed
            human_pets += st.human_animals_dropped
            turned += st.rotated
            job.current = "grouping people & pets…"
            fc.cluster_faces(conn, self.cfg)
            pc.cluster_pets(conn, self.cfg, root_id=job.root_id)
        people = conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
        groups = conn.execute("SELECT COUNT(*) FROM pets").fetchone()[0]
        job.message = (
            f"{faces_found} faces · {people} people · {animals} animals · "
            f"{groups} pet groups"
            + (f" · {suppressed} animal-face FPs dropped" if suppressed else "")
            + (f" · {human_pets} people misread as pets" if human_pets else "")
            + (f" · {turned} photos turned upright" if turned else ""))

    def _run_face_cluster(self, conn, job: Job, cancel):
        from ..faces.cluster import cluster_faces
        job.current = "reclustering people after review…"
        stats = cluster_faces(conn, self.cfg)
        job.done = job.total = 1
        job.message = f"{stats.people} people · {stats.clustered} faces clustered"

    def _run_semantic(self, job: Job, cancel):
        # Drain in passes so semantic indexing is ONE continuous job: each pass
        # handles the current backlog, then loops to pick up anything that became
        # pending while it ran (files the concurrent scan/enrich just added).
        # Restarting a fresh job per snapshot is what used to flicker the card
        # done→running; looping here keeps it steadily "running" until drained.
        total_indexed = total_skipped = total_failed = 0
        force = job.force
        while True:
            if cancel.is_set():
                raise KeyboardInterrupt
            indexed, skipped, failed, remaining = self._semantic_pass(job, cancel, force)
            total_indexed += indexed
            total_skipped += skipped
            total_failed += failed
            if remaining == 0:
                break
            force = False   # only the first pass honours a forced full reindex
        job.message = (
            f"{total_indexed} indexed, {total_skipped} skipped, {total_failed} errors"
            if (total_indexed or total_skipped or total_failed)
            else "semantic index is already current")

    def _semantic_pass(self, job: Job, cancel, force: bool):
        """One snapshot pass. Returns (indexed, skipped, failed, rows_in_pass)."""
        from pathlib import Path
        from . import semantic

        # Snapshot candidates under a read-only connection. The API calls below
        # happen without the writer lock so local metadata/faces work continues.
        read_conn = db.open_readonly(self.cfg.archive_db_path(job.root_id))
        try:
            rows = semantic.pending_rows(read_conn, job.root_id, force=force)
            total, already = semantic.work_counts(read_conn, job.root_id, force=force)
        finally:
            read_conn.close()
        job.total, job.done = total, already
        if not rows:
            return (0, 0, 0, 0)
        indexed = skipped = failed = 0
        # Voyage accepts up to 1,000 inputs per call. Keep requests deliberately
        # smaller: a few original high-resolution images can otherwise exceed
        # its 320K-token request ceiling. A failed multi-item request falls
        # back to individual requests, so one malformed source never holds up
        # the rest of an archive.
        batch_size = 20
        for start in range(0, len(rows), batch_size):
            if cancel.is_set():
                raise KeyboardInterrupt
            group = rows[start:start + batch_size]
            job.current = f"Preparing {len(group)} media files…"
            prepared = []
            for offset, row in enumerate(group):
                job.current = row["rel_path"]
                part, kind, reason = semantic.media_part(
                    self.cfg, Path(row["root_path"]) / row["rel_path"],
                    row["ext"], row["media_type"],
                    self.cfg.archive_cache_dir(job.root_id), row["rotate_deg"])
                if reason:
                    self._save_semantic_outcome(job.root_id, row, None, kind, reason)
                    skipped += 1
                    job.done = already + start + offset + 1
                else:
                    prepared.append((row, part, kind, offset))
            # Video token counts depend on sampled frames, so never combine
            # them with other files. Thumbnails have a bounded pixel count and
            # are safe to submit together.
            api_groups = [[item] for item in prepared if item[2] == "video"]
            images = [item for item in prepared if item[2] != "video"]
            api_groups += [images[n:n + batch_size] for n in range(0, len(images), batch_size)]
            for api_group in api_groups:
                if len(api_group) == 1 and api_group[0][2] == "video":
                    job.current = f"Sending video: {api_group[0][0]['rel_path']}"
                else:
                    job.current = f"Sending {len(api_group)} prepared photos to Voyage…"
                try:
                    vectors = semantic.embed_parts(self.cfg, [p[1] for p in api_group])
                    outcomes = zip(api_group, vectors)
                except Exception:
                    # Fall back to isolated calls; this identifies and records
                    # a bad source without discarding good neighbours.
                    outcomes = []
                    for item in api_group:
                        try:
                            job.current = f"Retrying: {item[0]['rel_path']}"
                            outcomes.append((item, semantic.embed_parts(self.cfg, [item[1]])[0]))
                        except Exception as exc:
                            self._save_semantic_outcome(job.root_id, item[0], None, item[2], str(exc))
                            failed += 1
                            job.done = already + start + item[3] + 1
                for (row, _part, kind, offset), values in outcomes:
                    self._save_semantic_outcome(job.root_id, row, values, kind, None)
                    indexed += 1
                    job.done = already + start + offset + 1
        return (indexed, skipped, failed, len(rows))

    def _save_semantic_outcome(self, root_id, row, values, kind, reason):
        """Persist one result without taking the manager-wide pipeline lock.

        SQLite WAL plus its busy timeout serializes this tiny transaction against
        a metadata/faces batch, while the semantic worker remains independent of
        that job's long-lived manager lock.
        """
        from . import semantic
        conn = db.connect(self.cfg.archive_db_path(root_id))
        try:
            # Dedup may have completed while Voyage was processing this item.
            # Only keep an outcome if this exact source remains canonical.
            current = conn.execute(
                "SELECT hidden, sha256 FROM files WHERE id=?", (row["id"],)
            ).fetchone()
            if (current is None or current["hidden"] or
                    current["sha256"] != row["sha256"]):
                return
            semantic.save_outcome(conn, self.cfg, row, values, kind, reason)
            conn.commit()
        finally:
            conn.close()
