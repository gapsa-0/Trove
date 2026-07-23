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
    kind: str                 # "scan" | "enrich"
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
    # Images per detect-then-recluster chunk in a faces job (see _run_faces):
    # small enough that people appear early in a multi-hour run, large enough
    # that repeated clustering stays a small fraction of total time.
    _FACE_CHUNK = 1200

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._jobs: dict[int, Job] = {}
        self._cancels: dict[int, threading.Event] = {}
        self._seq = 0
        self._lock = threading.Lock()          # guards registry
        self._write_lock = threading.Lock()    # serializes DB writers

        # Both dedup and map place-clustering are "derived" passes rerun whenever
        # new/enriched data lands; dirty flags queue them once an archive is open.
        self._dedup_dirty = True
        self._dedup_root: int | None = None
        self._places_dirty = True
        self._places_root: int | None = None
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

    def get(self, job_id: int) -> dict | None:
        j = self._jobs.get(job_id)
        return j.public() if j else None

    def active_kind(self, kind: str) -> bool:
        return any(j.status == "running" and j.kind == kind
                   for j in self._jobs.values())

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
    # pipeline (scan → enrich → faces → dedup) for it. Closing or switching the
    # archive requests cancellation; long jobs commit in batches and resume the
    # next time that archive is opened.
    def open_archive(self, root_id: int):
        """Allow automatic work for this archive while it is being viewed."""
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
        """One scheduling decision. Returns True if it started (or is waiting
        on) work, so the caller can poll again soon rather than backing off."""
        from . import queries, semantic
        open_root_id = self._open_root_id
        if open_root_id is None:
            return False
        archives = [a for a in queries.archives(self.cfg.db_path)
                    if a["id"] == open_root_id and a["exists"]]
        if not archives:
            return False
        # Do not embed until dedup has selected canonicals. Once that is done,
        # Remote semantic calls may overlap local metadata/faces extraction safely.
        dedup_ready = not self._dedup_dirty and not self.active_kind("dedup")
        if dedup_ready and semantic.api_key_available() and not self.active_kind("semantic"):
            for archive in archives:
                if queries.semantic_pending(self.cfg.db_path, archive["id"]):
                    self.start("semantic", archive["id"], archive["path"])
                    return True
        # A scan owns a small companion metadata worker, which reads each committed
        # scan batch while hashing proceeds. All other local stages stay exclusive.
        # Semantic indexing spends almost all of its time waiting on Voyage and
        # only locks SQLite for one completed-result write at a time. It must not
        # stall the local scan/enrich/faces pipeline while those requests run.
        if any(j.status == "running" and j.kind != "semantic"
               for j in self._jobs.values()):
            return True
        for a in archives:
            fresh = queries.freshness(self.cfg.db_path, a["id"])
            if fresh.get("new_files", 0) > 0:
                self.start("scan", a["id"], a["path"])
                self._dedup_dirty, self._dedup_root = True, a["id"]
                self._places_dirty, self._places_root = True, a["id"]
                return True
            if fresh.get("not_enriched", 0) > 0:
                self.start("enrich", a["id"], a["path"])
                self._dedup_dirty, self._dedup_root = True, a["id"]
                self._places_dirty, self._places_root = True, a["id"]
                return True
        # Dedup runs BEFORE faces on purpose: it sets files.hidden on duplicate
        # copies, and the face pass skips hidden files — so faces only ever run on
        # unique/canonical images (no wasted detection on duplicate photos).
        if self._dedup_dirty:
            self._dedup_dirty = False
            root_id = open_root_id
            self.start("dedup", root_id, None)
            return True
        # Faces: the long extraction pass, so it runs after the cheap ones.
        # Skipped entirely when the local face backend isn't available, so we
        # never spin on work that can't run.
        from ..faces import backend as face_backend
        if face_backend.available():
            for a in archives:
                if queries.faces_pending(self.cfg.db_path, a["id"]) > 0:
                    self.start("faces", a["id"], a["path"])
                    return True
        # Map place-clustering: rebuild whenever new geo data landed, so the Map
        # stays in sync on its own (it used to refresh only via a manual button).
        if self._places_dirty:
            self._places_dirty = False
            self.start("places", open_root_id, None)
            return True
        return False

    # -- worker -----------------------------------------------------------
    def _run(self, job: Job, cancel: threading.Event):
        try:
            if job.kind == "semantic":
                self._run_semantic(job, cancel)
            elif job.kind == "scan":
                # A scan is the one exception to the long-held writer lock:
                # _run_scan starts a metadata worker which uses a separate
                # connection. SQLite/WAL still serializes their brief commits.
                conn = db.connect(self.cfg.db_path)
                db.init_db(conn)
                try:
                    self._run_scan(conn, job, cancel)
                finally:
                    conn.close()
            else:
                with self._write_lock:
                    conn = db.connect(self.cfg.db_path)
                    db.init_db(conn)
                    try:
                        if job.kind == "scan":
                            self._run_scan(conn, job, cancel)
                        elif job.kind == "enrich":
                            self._run_enrich(conn, job, cancel)
                        elif job.kind == "dedup":
                            self._run_dedup(conn, job, cancel)
                        elif job.kind == "places":
                            self._run_places(conn, job, cancel)
                        elif job.kind == "faces":
                            self._run_faces(conn, job, cancel)
                        else:
                            raise ValueError(f"unknown job kind: {job.kind}")
                    finally:
                        conn.close()
            job.status = "done"
        except KeyboardInterrupt:
            job.status = "cancelled"
            job.message = "cancelled — progress saved"
        except Exception as e:
            job.status = "error"
            job.message = f"{e}"
            traceback.print_exc()
        finally:
            job.finished_at = time.time()

    def _run_scan(self, conn, job: Job, cancel):
        from pathlib import Path
        from ..metadata import enrich as enrich_mod

        prog = _JobProgress(job, cancel)
        run_started = db.now_iso()
        roots = [job.root_path] if job.root_path else list(self.cfg.roots)
        prog.total = sum(
            walker.count_files(__import__("pathlib").Path(r))
            for r in roots if __import__("pathlib").Path(r).is_dir()
        )
        # Establish roots before starting the companion worker. It only sees
        # batches after scan_root commits them, and it is deliberately confined
        # to this scan's roots so other archives keep their normal scheduling.
        root_ids = tuple(
            db.get_or_create_root(conn, str(Path(r)))
            for r in roots if Path(r).is_dir()
        )
        scan_finished = threading.Event()
        metadata_error: list[BaseException] = []
        metadata_stats = enrich_mod.EnrichStats()

        def enrich_while_scanning():
            nonlocal metadata_stats
            meta_conn = db.connect(self.cfg.db_path)
            try:
                while True:
                    if cancel.is_set():
                        raise KeyboardInterrupt
                    # Enrichment itself commits every 80 files, keeping write
                    # transactions short and yielding often to the hash scan.
                    # Once scanning finishes, keep draining its final batches.
                    stats = enrich_mod.enrich(
                        meta_conn, self.cfg, batch_size=80, root_ids=root_ids,
                    )
                    metadata_stats.processed += stats.processed
                    metadata_stats.with_takeout += stats.with_takeout
                    metadata_stats.with_gps += stats.with_gps
                    if scan_finished.is_set() and stats.processed == 0:
                        return
                    scan_finished.wait(0.15)
            except BaseException as exc:
                metadata_error.append(exc)
            finally:
                meta_conn.close()

        metadata_thread = None
        if root_ids:
            metadata_thread = threading.Thread(target=enrich_while_scanning, daemon=True)
            metadata_thread.start()
        base = 0
        try:
            for r in roots:
                stats = walker.scan_root(conn, self.cfg, r, run_started,
                                         progress=prog, base_done=base,
                                         # Publish small batches so metadata can
                                         # begin while the scanner hashes later files.
                                         commit_every=80)
                base += stats.seen
        finally:
            scan_finished.set()
            if metadata_thread is not None:
                metadata_thread.join()
        if metadata_error:
            raise metadata_error[0]
        job.message = f"{base} files scanned · {metadata_stats.processed} metadata extracted"

    def _run_enrich(self, conn, job: Job, cancel):
        from ..metadata import enrich as enrich_mod
        prog = _JobProgress(job, cancel)
        stats = enrich_mod.enrich(conn, self.cfg, progress=prog)
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
        from ..geo.clusters import cluster_places, assign_unplaced
        from . import queries
        roots = [a for a in queries.archives(self.cfg.db_path) if a["exists"]]
        job.total = len(roots)
        touched = 0
        for i, a in enumerate(roots):
            job.current = a["name"]
            has_places = conn.execute(
                "SELECT 1 FROM place_clusters WHERE root_id=? LIMIT 1", (a["id"],)
            ).fetchone()
            if has_places:
                touched += assign_unplaced(conn, a["id"]).points
            else:
                cluster_places(conn, a["id"])
            job.done = i + 1
        job.message = f"{touched} new geotagged files placed"

    def _run_faces(self, conn, job: Job, cancel):
        # Part of the automatic pipeline (runs after scan + enrich). Detect faces
        # in chunks and re-cluster after each chunk, so the Faces section fills
        # in progressively during a long run instead of only at the very end.
        from ..faces import extract as fx, cluster as fc
        # Progress is cumulative over ALL unique images, not just this run's
        # backlog: total = every canonical image, done starts at how many are
        # already scanned. So the bar/% match the "Scanned N / total" stat tile
        # and survive resuming across app restarts (no misleading per-run total).
        total = fx.image_count(conn)
        already = max(0, total - fx.pending_count(conn))
        job.total, job.done = total, already
        # Load the face models once and reuse them across every chunk.
        be = fx.make_backend(self.cfg, log=lambda m: setattr(job, "current", m))
        processed = faces_found = 0
        while True:
            if cancel.is_set():
                raise KeyboardInterrupt
            prog = _JobProgress(job, cancel, base=already + processed, fixed_total=True)
            es = fx.extract(conn, self.cfg, progress=prog, limit=self._FACE_CHUNK, be=be)
            if es.processed == 0:
                break
            processed += es.processed
            faces_found += es.faces_found
            job.current = "clustering people…"
            fc.cluster_faces(conn, self.cfg)
        people = conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
        job.message = f"{faces_found} faces in {processed} photos · {people} people"

    def _run_semantic(self, job: Job, cancel):
        from pathlib import Path
        from . import semantic

        # Snapshot candidates under a read-only connection. The API calls below
        # happen without the writer lock so local metadata/faces work continues.
        read_conn = db.open_readonly(self.cfg.db_path)
        try:
            rows = semantic.pending_rows(read_conn, job.root_id, force=job.force)
            total, already = semantic.work_counts(read_conn, job.root_id, force=job.force)
        finally:
            read_conn.close()
        job.total, job.done = total, already
        if not rows:
            job.message = "semantic index is already current"
            return
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
                    row["ext"], row["media_type"])
                if reason:
                    self._save_semantic_outcome(row, None, kind, reason)
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
                            self._save_semantic_outcome(item[0], None, item[2], str(exc))
                            failed += 1
                            job.done = already + start + item[3] + 1
                for (row, _part, kind, offset), values in outcomes:
                    self._save_semantic_outcome(row, values, kind, None)
                    indexed += 1
                    job.done = already + start + offset + 1
        job.message = f"{indexed} indexed, {skipped} skipped, {failed} errors"

    def _save_semantic_outcome(self, row, values, kind, reason):
        """Persist one result without taking the manager-wide pipeline lock.

        SQLite WAL plus its busy timeout serializes this tiny transaction against
        a metadata/faces batch, while the semantic worker remains independent of
        that job's long-lived manager lock.
        """
        from . import semantic
        conn = db.connect(self.cfg.db_path)
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
