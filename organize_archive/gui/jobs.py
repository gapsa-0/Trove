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

        self._auto_paused = False
        self._dedup_dirty = True   # run one dedup pass on startup
        self._dedup_root: int | None = None
        self._auto_interval = self._AUTO_MIN
        threading.Thread(target=self._auto_loop, daemon=True).start()

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

    # -- control ----------------------------------------------------------
    def start(self, kind: str, root_id: int | None = None,
              root_path: str | None = None) -> dict:
        if self.active_kind(kind):
            return {"error": f"a {kind} job is already running"}
        with self._lock:
            self._seq += 1
            job = Job(id=self._seq, kind=kind, root_id=root_id, root_path=root_path)
            self._jobs[job.id] = job
            cancel = threading.Event()
            self._cancels[job.id] = cancel
        t = threading.Thread(target=self._run, args=(job, cancel), daemon=True)
        t.start()
        return job.public()

    def cancel(self, job_id: int) -> bool:
        ev = self._cancels.get(job_id)
        if ev:
            ev.set()
            return True
        return False

    # -- automatic scheduling ----------------------------------------------
    # The catalog is meant to keep itself in sync without the user pressing
    # buttons: a background thread notices new/un-enriched files and starts
    # the right job on its own. "Pause" just stops it from starting new work;
    # it doesn't interrupt a job already running.
    def auto_status(self) -> dict:
        return {"paused": self._auto_paused}

    def set_auto_paused(self, paused: bool):
        self._auto_paused = bool(paused)
        if not self._auto_paused:
            self._auto_interval = self._AUTO_MIN  # check promptly after resuming

    def _auto_loop(self):
        while True:
            time.sleep(self._auto_interval)
            if self._auto_paused:
                continue
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
        from . import queries
        if any(j.status == "running" for j in self._jobs.values()):
            return True
        archives = queries.archives(self.cfg.db_path)
        for a in archives:
            if not a["exists"]:
                continue
            fresh = queries.freshness(self.cfg.db_path, a["id"])
            if fresh.get("new_files", 0) > 0:
                self.start("scan", a["id"], a["path"])
                self._dedup_dirty, self._dedup_root = True, a["id"]
                return True
            if fresh.get("not_enriched", 0) > 0:
                self.start("enrich", a["id"], a["path"])
                self._dedup_dirty, self._dedup_root = True, a["id"]
                return True
        # Faces come after every archive is scanned + enriched (lowest-priority
        # extraction pass, before dedup). Skipped entirely when the local face
        # backend isn't available, so we never spin on work that can't run.
        from ..faces import backend as face_backend
        if face_backend.available():
            for a in archives:
                if not a["exists"]:
                    continue
                if queries.faces_pending(self.cfg.db_path, a["id"]) > 0:
                    self.start("faces", a["id"], a["path"])
                    return True
        if self._dedup_dirty:
            self._dedup_dirty = False
            # Dedup itself spans every archive, but the job is tagged with one
            # root_id so its progress bar shows up somewhere (the GUI's job
            # list is filtered per-archive) — prefer the archive that just got
            # new/enriched data, else fall back to whichever archive exists.
            root_id = self._dedup_root
            if root_id is None:
                root_id = next((a["id"] for a in archives if a["exists"]), None)
            self.start("dedup", root_id, None)
            return True
        return False

    # -- worker -----------------------------------------------------------
    def _run(self, job: Job, cancel: threading.Event):
        try:
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
        prog = _JobProgress(job, cancel)
        run_started = db.now_iso()
        roots = [job.root_path] if job.root_path else list(self.cfg.roots)
        prog.total = sum(
            walker.count_files(__import__("pathlib").Path(r))
            for r in roots if __import__("pathlib").Path(r).is_dir()
        )
        base = 0
        for r in roots:
            stats = walker.scan_root(conn, self.cfg, r, run_started,
                                     progress=prog, base_done=base)
            base += stats.seen
        job.message = f"{base} files scanned"

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
        stats = exact.run(conn, progress=prog)
        job.message = (f"{stats.groups} groups, {stats.duplicate_files} duplicates, "
                       f"{stats.reclaimable_bytes/1e9:.1f} GB reclaimable")

    def _run_faces(self, conn, job: Job, cancel):
        # Part of the automatic pipeline (runs after scan + enrich). Detect faces
        # in chunks and re-cluster after each chunk, so the Faces section fills
        # in progressively during a long run instead of only at the very end.
        from ..faces import extract as fx, cluster as fc
        job.total = fx.pending_count(conn)
        # Load the face models once and reuse them across every chunk.
        be = fx.make_backend(self.cfg, log=lambda m: setattr(job, "current", m))
        processed = faces_found = 0
        while True:
            if cancel.is_set():
                raise KeyboardInterrupt
            prog = _JobProgress(job, cancel, base=processed, fixed_total=True)
            es = fx.extract(conn, self.cfg, progress=prog, limit=self._FACE_CHUNK, be=be)
            if es.processed == 0:
                break
            processed += es.processed
            faces_found += es.faces_found
            job.current = "clustering people…"
            fc.cluster_faces(conn, self.cfg)
        people = conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
        job.message = f"{faces_found} faces in {processed} photos · {people} people"
