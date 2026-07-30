"""SQLite access layer.

Thin wrapper around sqlite3 so the storage backend stays swappable. Uses WAL
mode and a schema version tracked via ``PRAGMA user_version``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 13
_SCHEMA_SQL = Path(__file__).with_name("schema.sql")


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    # busy_timeout MUST be set before any statement that can take a lock. The GUI
    # runs a near-continuous background pipeline that holds the single writer in
    # bursts, while HTTP handler threads issue small writes (rename a person, set a
    # date, attach a place). If busy_timeout is set *after* `PRAGMA journal_mode=WAL`,
    # that pragma — which itself can need a lock — runs under only Python's short
    # default timeout and fails outright with "database is locked" the moment it
    # overlaps a pipeline write. Setting it first makes every later statement wait
    # (retry) for the writer instead of erroring.
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def write_with_retry(fn, *, retries: int = 4, initial_delay: float = 0.25):
    """Call ``fn()``, retrying with backoff if SQLite reports a locked writer.

    ``fn`` takes no arguments and performs one bounded write, including its own
    commit. ``busy_timeout`` (set in ``connect``) already makes a single write
    wait out most overlaps with the pipeline's writer before ever raising; this
    covers the rarer case where a write still loses that race (e.g. a batch
    that holds the writer past the busy_timeout window). A caller that must
    not fail outright on a persistent lock should catch
    ``sqlite3.OperationalError`` around this call and decide what "leave it
    for later" means for that write, instead of letting a lock become a stage
    or request failure.
    """
    delay = initial_delay
    for attempt in range(retries + 1):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if "database is locked" not in str(exc).lower() or attempt == retries:
                raise
            time.sleep(delay)
            delay *= 2


def open_readonly(db_path: str | Path) -> sqlite3.Connection:
    """Open a read-only connection safe to use while a scan is writing.

    Uses a normal connection (so it can read not-yet-checkpointed WAL data) but
    sets ``query_only`` so it never takes a write lock and never contends with
    the single writer.
    """
    conn = connect(db_path)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _add_column_if_missing(conn, table, column, decl):
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db(conn: sqlite3.Connection) -> None:
    """Create/upgrade the schema. Idempotent."""
    # Read before touching anything: executescript's CREATE TABLE/INDEX IF NOT
    # EXISTS statements never change user_version, so this is a true "what did
    # this file last see" marker for gating one-time migrations below. This
    # call runs at every job start (see web/jobs.py), so anything gated on it
    # must stay cheap once the database is already current -- a version check
    # is one page read, not a table scan.
    previous_version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.executescript(_SCHEMA_SQL.read_text())
    # Migrations for columns added to existing tables.
    _add_column_if_missing(conn, "files", "dup_group_id", "INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_dupgroup ON files(dup_group_id)")
    # Durable-place / editable-detail support:
    #  - place_cluster_members.source distinguishes GPS-derived ('auto') members from
    #    ones the user attached by hand ('manual'); manual members are never wiped.
    #  - place_clusters.pinned marks a user-created place whose coordinate is a fixed
    #    pin (never recomputed from members).
    #  - faces.manual_person pins a face to a person *by name* (the only identity stable
    #    across the DELETE/rebuild in faces/cluster.py), re-applied after every recluster.
    # scan_runs gains the two things that let a *finished* scan be recognised as
    # finished: which root it covered, and how many files were on disk when it
    # completed (see scan_settled).
    _add_column_if_missing(conn, "scan_runs", "root_id", "INTEGER")
    _add_column_if_missing(conn, "scan_runs", "files_on_disk", "INTEGER")
    _add_column_if_missing(conn, "place_cluster_members", "source", "TEXT DEFAULT 'auto'")
    conn.execute("UPDATE place_cluster_members SET source='auto' WHERE source IS NULL")
    _add_column_if_missing(conn, "place_clusters", "pinned", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "faces", "manual_person", "TEXT")
    # persons.centroid: cached L2-normalized mean embedding, used to suggest
    # "same person?" merges without reloading every embedding.
    _add_column_if_missing(conn, "persons", "centroid", "BLOB")
    # faces.not_person: user marked this cluster as not a person (doll/animal/
    # cartoon face that YuNet detected); excluded from clustering thereafter.
    _add_column_if_missing(conn, "faces", "not_person", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "faces", "nonhuman_kind", "TEXT")
    _add_column_if_missing(conn, "faces", "nonhuman_source", "TEXT")
    # Face-quality metrics are stored with algorithm provenance. Scan-level
    # rejection counters make quality-gate behavior inspectable without saving
    # rejected doll/cartoon/blurry candidates as faces.
    _add_column_if_missing(conn, "faces", "focus_score", "REAL")
    _add_column_if_missing(conn, "faces", "brightness", "REAL")
    _add_column_if_missing(conn, "faces", "extreme_fraction", "REAL")
    _add_column_if_missing(conn, "faces", "clipped_fraction", "REAL")
    _add_column_if_missing(conn, "faces", "quality_score", "REAL")
    _add_column_if_missing(conn, "faces", "quality_source", "TEXT")
    # FIQA gate (faces/fiqa.py). Pre-existing rows have no norm to score, so they
    # are left with a NULL tier, which every consumer reads as BORDERLINE: still
    # clustered and still visible, never used to seed a core. That is the safe
    # reading for a face whose quality is simply unknown, and it keeps an
    # un-migrated database working until the AdaFace re-extract fills the column.
    _add_column_if_missing(conn, "faces", "fiqa_norm", "REAL")
    _add_column_if_missing(conn, "faces", "fiqa_score", "REAL")
    _add_column_if_missing(conn, "faces", "fiqa_source", "TEXT")
    _add_column_if_missing(conn, "faces", "quality_tier", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_faces_tier ON faces(quality_tier)")
    _add_column_if_missing(conn, "face_scan", "n_candidates", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "face_scan", "rejected_score", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "face_scan", "rejected_size", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "face_scan", "rejected_focus", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "face_scan", "rejected_exposure", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "face_scan", "rejected_clipped", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "face_scan", "rejected_nonhuman", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "nonhuman_detections", "embedding", "BLOB")
    _add_column_if_missing(conn, "nonhuman_detections", "source_sha256", "TEXT")
    _add_column_if_missing(conn, "nonhuman_detections", "det_score", "REAL")
    _add_column_if_missing(conn, "nonhuman_detections", "focus_score", "REAL")
    _add_column_if_missing(conn, "nonhuman_detections", "brightness", "REAL")
    _add_column_if_missing(conn, "nonhuman_detections", "extreme_fraction", "REAL")
    _add_column_if_missing(conn, "nonhuman_detections", "clipped_fraction", "REAL")
    _add_column_if_missing(conn, "nonhuman_detections", "quality_score", "REAL")
    _add_column_if_missing(conn, "nonhuman_detections", "quality_source", "TEXT")
    _add_column_if_missing(
        conn, "nonhuman_detections", "review_status", "TEXT NOT NULL DEFAULT 'pending'"
    )
    _add_column_if_missing(conn, "nonhuman_detections", "restored_face_id", "INTEGER")
    _add_column_if_missing(conn, "pet_scan", "source_sha256", "TEXT")
    # Detection now also runs on videos, via sampled keyframes. A box on a video
    # is meaningless without the frame it was found in, so both detection tables
    # record the ffmpeg offset to re-extract that frame for cropping.
    _add_column_if_missing(conn, "faces", "frame_offset", "TEXT")
    _add_column_if_missing(conn, "animal_detections", "frame_offset", "TEXT")
    _add_column_if_missing(conn, "semantic_embeddings", "indexer_version", "TEXT")
    if previous_version < 12:
        # One-time cleanup, gated to schema version 12 so it runs exactly once
        # ever instead of on every init_db call (init_db runs at every job
        # start -- an unconditional DELETE here was a full-table write taking
        # the single writer's lock on every job, independent of whether there
        # was anything to clean up). Earlier runs embedded a video's raw bytes
        # wholesale (input_kind='video'), which busts Voyage's context window
        # on anything but a tiny clip and so failed identically forever.
        # Frame-sampled video indexing (services/semantic.py) writes a new
        # input_kind ('video_frames'), so this DELETE only ever matches the
        # old rows; rows with input_kind='video' become pending again exactly
        # once, the first time a pre-12 database is opened.
        conn.execute("DELETE FROM semantic_embeddings WHERE input_kind='video'")
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()


def get_or_create_root(conn: sqlite3.Connection, path: str) -> int:
    row = conn.execute("SELECT id FROM roots WHERE path=?", (path,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO roots(path, added_at) VALUES(?, ?)", (path, now_iso()))
    conn.commit()
    return cur.lastrowid


# Tables that key rows to a root directly, so a renumbered root takes them
# along. Deleting a root is different: only `files` needs clearing by hand,
# since the other two cascade and everything else hangs off a file id.
_ROOT_SCOPED_TABLES = ("files", "dedup_runs", "place_clusters")


def reconcile_root(conn: sqlite3.Connection, root_id: int, path: str) -> bool:
    """Make ``root_id`` the one and only root of this per-archive database.

    Each isolated archive database has exactly one root, and its id is kept
    equal to the archive's id in the app registry — every place that already
    threads a ``root_id`` through (jobs, queries, URLs) keeps meaning the same
    thing without change; only *which database file* it points at changes.

    Nothing enforced that before, and once the two disagree the app goes quiet
    rather than loud: the scanner resolves its root *by path* and happily
    creates a second one, so every scanned file lands under an id no query ever
    asks for. The archive then reads as almost empty, and — because the scan
    stage measures its backlog as "files on disk minus rows for this root" —
    the pipeline rescans the whole archive forever, restarting its progress
    each time. Two ways in, both reachable from a normal install: an archive id
    is reused after a removal that left the old directory behind, and the
    legacy migration copies a pre-isolation database keeping *its* root id.

    So this is called on the way in to every archive, and it repairs rather
    than reports: rows under a second root for the same folder are adopted
    (they describe this archive's files and were expensive to build), and rows
    belonging to some *other* folder are dropped, since a per-archive database
    has no business holding them. Returns True when it changed anything, so
    callers can invalidate work derived from the old shape.
    """
    rows = {
        r["id"]: (r["path"], r["added_at"])
        for r in conn.execute("SELECT id, path, added_at FROM roots")
    }
    if {rid: p for rid, (p, _) in rows.items()} == {root_id: path}:
        return False

    # The row that already describes this folder, whatever id it ended up with.
    owner = next((rid for rid, (p, _) in rows.items() if p == path), None)
    keep = {root_id, owner} - {None}
    placeholder = f"{path}\x00reconciling"
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 1. Roots for other folders (and their catalog rows) do not belong here.
        stale = [rid for rid in rows if rid not in keep]
        for rid in stale:
            conn.execute("DELETE FROM files WHERE root_id=?", (rid,))
            conn.execute("DELETE FROM roots WHERE id=?", (rid,))
        if owner is None:
            # A row may still be sitting on the target id under a stale path.
            conn.execute("DELETE FROM files WHERE root_id=?", (root_id,))
            conn.execute("DELETE FROM roots WHERE id=?", (root_id,))
            conn.execute(
                "INSERT INTO roots(id, path, added_at) VALUES(?,?,?)", (root_id, path, now_iso())
            )
        elif owner != root_id:
            # Renumber the owning root onto the archive's id, taking its files
            # with it. The placeholder keeps `roots.path` unique for the moment
            # both rows exist, which is what lets the whole move happen without
            # ever leaving a file row pointing at a root that isn't there —
            # no deferred foreign keys needed.
            conn.execute("DELETE FROM files WHERE root_id=?", (root_id,))
            conn.execute("DELETE FROM roots WHERE id=?", (root_id,))
            conn.execute("UPDATE roots SET path=? WHERE id=?", (placeholder, owner))
            conn.execute(
                "INSERT INTO roots(id, path, added_at) VALUES(?,?,?)",
                (root_id, path, rows[owner][1]),
            )
            for table in _ROOT_SCOPED_TABLES:
                conn.execute(f"UPDATE {table} SET root_id=? WHERE root_id=?", (root_id, owner))
            conn.execute("DELETE FROM roots WHERE id=?", (owner,))
        conn.commit()
    except Exception:
        # Roll back and re-raise deliberately: this is not silent, the caller
        # (up to the cli.py boundary) logs the failure -- logging here too would
        # record the same failure twice.
        conn.rollback()
        raise
    return True


# ---- Scan completion ("has this root been walked as it stands") ------------
#
# The scan stage sizes its backlog as "files on disk minus rows for this root",
# which is a fine progress estimate and a terrible completion test: a file the
# scanner cannot stat or hash is counted on disk and never becomes a row, so
# the difference stays positive no matter how many times the archive is walked
# — and the scheduler starts another scan the moment the last one ends, forever,
# resetting the progress bar each time. What actually settles the question is
# that a scan ran to completion and nothing has changed on disk since, which is
# what these record and answer.


def scan_run_start(conn: sqlite3.Connection, root_id: int, roots) -> int:
    cur = conn.execute(
        "INSERT INTO scan_runs(started_at, roots, root_id) VALUES(?,?,?)",
        (now_iso(), json.dumps(list(roots)), root_id),
    )
    conn.commit()
    return cur.lastrowid


def scan_run_finish(
    conn: sqlite3.Connection, run_id: int, stats, files_on_disk: int | None
) -> None:
    """Close out a scan that walked the whole root. Only ever called on the
    normal path — an interrupted scan leaves ``finished_at`` NULL so it is not
    mistaken for full coverage."""
    conn.execute(
        """UPDATE scan_runs SET finished_at=?, files_seen=?, files_new=?,
           files_updated=?, bytes_hashed=?, files_on_disk=? WHERE id=?""",
        (
            now_iso(),
            stats.seen,
            stats.new,
            stats.updated,
            stats.bytes_hashed,
            files_on_disk,
            run_id,
        ),
    )
    conn.commit()


def scan_settled(conn: sqlite3.Connection, root_id: int, files_on_disk: int | None) -> bool:
    """True when a completed scan already covers exactly what is on disk now.

    ``files_on_disk`` is the current count; comparing it with the one the last
    completed scan saw also catches deletions, which a backlog subtraction
    silently floors at zero.
    """
    if files_on_disk is None:
        return False
    row = conn.execute(
        """SELECT files_on_disk FROM scan_runs
           WHERE root_id=? AND finished_at IS NOT NULL AND files_on_disk IS NOT NULL
           ORDER BY id DESC LIMIT 1""",
        (root_id,),
    ).fetchone()
    return row is not None and row["files_on_disk"] == files_on_disk


# ---- Dedup rebuild coverage (catalog-derived "is a rebuild owed") ----------
#
# A wholesale dedup rebuild has no per-file backlog to count the way
# enrich/faces/semantic do, so "is a rebuild owed" is instead answered by
# comparing what the last successful rebuild covered against what the catalog
# looks like now. Persisting that comparison point (`dedup_runs`) instead of
# keeping it in an in-memory flag is what makes the answer survive a restart:
# a freshly constructed JobManager re-derives the same answer a live one
# would have given, rather than defaulting to "rebuild everything" the moment
# the process restarts.


def dedup_coverage(conn: sqlite3.Connection, root_id: int) -> tuple[int, int | None]:
    """(count, max id) of files eligible for dedup grouping under this root:
    present, content-hashed files -- the same population dedup/exact.py's
    `run()` groups. `None` for the max id means there are no such files yet.
    """
    row = conn.execute(
        "SELECT COUNT(*), MAX(id) FROM files WHERE present=1 AND sha256 IS NOT NULL AND root_id=?",
        (root_id,),
    ).fetchone()
    return row[0], row[1]


def dedup_mark_done(
    conn: sqlite3.Connection, root_id: int, covered_files: int, covered_max_file_id: int | None
) -> None:
    """Record a successful rebuild's coverage.

    Its caller (``jobs._run_dedup``) writes this on the rebuild's connection but
    in a later transaction than ``exact.run``'s own commit, so the marker can
    lag the grouping it describes if the process dies in that narrow window.
    That direction is the safe one: the grouping is already correct and the only
    cost is one redundant rebuild. Never write it *before* the grouping commits,
    which would strand a stale grouping behind an up-to-date marker."""
    conn.execute(
        """INSERT INTO dedup_runs(root_id, covered_files, covered_max_file_id, run_at)
           VALUES(?, ?, ?, ?)
           ON CONFLICT(root_id) DO UPDATE SET
               covered_files=excluded.covered_files,
               covered_max_file_id=excluded.covered_max_file_id,
               run_at=excluded.run_at""",
        (root_id, covered_files, covered_max_file_id, now_iso()),
    )


def dedup_invalidate(conn: sqlite3.Connection, root_id: int) -> None:
    """Mark a rebuild as owed for this root again, persistently.

    Scan/enrich can change either the file set or metadata dedup's canonical
    -selection rule reads (Takeout sidecar, resolved date, dimensions)
    *without* changing the count/max id `dedup_needed` otherwise compares
    (enrich never touches `files.sha256`/`present`), so that comparison alone
    would miss a case where the previous grouping's canonical pick is now
    stale. Deleting the marker forces `dedup_needed` back to True regardless,
    until the next successful rebuild re-marks it.
    """
    conn.execute("DELETE FROM dedup_runs WHERE root_id=?", (root_id,))


def dedup_needed(conn: sqlite3.Connection, root_id: int) -> bool:
    """Whether a duplicate rebuild is outstanding for this root.

    No stored marker (never rebuilt, or explicitly invalidated -- see
    `dedup_invalidate`) means a rebuild is owed. Otherwise, owed exactly when
    the present, hashed file population has moved on from what the marker
    recorded.
    """
    row = conn.execute(
        "SELECT covered_files, covered_max_file_id FROM dedup_runs WHERE root_id=?",
        (root_id,),
    ).fetchone()
    if row is None:
        return True
    current_files, current_max_id = dedup_coverage(conn, root_id)
    return row[0] != current_files or row[1] != current_max_id
