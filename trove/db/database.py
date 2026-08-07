"""SQLite access layer.

Thin wrapper around sqlite3 so the storage backend stays swappable. Uses WAL
mode and a schema version tracked via ``PRAGMA user_version``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

# Re-exported rather than merely used: every caller reaches the database layer
# as ``db``, and moving the migrations into their own module is not a reason to
# make them ask a second module whether this build has FTS5.
from .migrations import fts5_supported, text_index_present
from .migrations import run as _run_migrations

__all__ = [
    "SCHEMA_VERSION",
    "connect",
    "dedup_coverage",
    "dedup_invalidate",
    "dedup_mark_done",
    "dedup_needed",
    "fts5_supported",
    "get_or_create_root",
    "init_db",
    "now_iso",
    "open_readonly",
    "present_file_count",
    "reconcile_root",
    "scan_run_finish",
    "scan_run_start",
    "scan_settled",
    "text_index_present",
    "write_with_retry",
]

SCHEMA_VERSION = 16
_SCHEMA_SQL = Path(__file__).with_name("schema.sql")


class _ScanStatsLike(Protocol):
    """The subset of scan.walker.ScanStats this module reads.

    A structural type instead of importing the real class: db/ is L0
    (foundation) and scan/ is L1 (domain) in the package layering that
    tests/unit/test_layering.py enforces, so db may not import from scan even
    under TYPE_CHECKING.
    """

    seen: int
    new: int
    updated: int
    bytes_hashed: int
    unstable: int


def now_iso() -> str:
    """Current UTC time as an ISO 8601 string, seconds precision."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a writable connection with WAL, busy_timeout and foreign keys set.

    Row access is by column name (``sqlite3.Row``). The pragmas below are
    issued in a load-bearing order -- see the comment on ``busy_timeout``.
    """
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


def write_with_retry[T](fn: Callable[[], T], *, retries: int = 4, initial_delay: float = 0.25) -> T:
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

    ``retries`` must not be negative. It used to be possible to call this with
    a negative count and have it perform **no write at all** and return None,
    silently -- the worst outcome a write helper can have, and invisible to
    every caller because they all take the default.
    """
    if retries < 0:
        raise ValueError(f"retries must be >= 0, got {retries}")
    delay = initial_delay
    for attempt in range(retries + 1):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if "database is locked" not in str(exc).lower() or attempt == retries:
                raise
            time.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable: the loop's last iteration returns or raises")


def open_readonly(db_path: str | Path) -> sqlite3.Connection:
    """Open a read-only connection safe to use while a scan is writing.

    Uses a normal connection (so it can read not-yet-checkpointed WAL data) but
    sets ``query_only`` so it never takes a write lock and never contends with
    the single writer.
    """
    conn = connect(db_path)
    conn.execute("PRAGMA query_only=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create/upgrade the schema. Idempotent."""
    # Read before touching anything: executescript's CREATE TABLE/INDEX IF NOT
    # EXISTS statements never change user_version, so this is a true "what did
    # this file last see" marker for gating one-time migrations below. This
    # call runs at every job start (see pipeline/manager.py), so anything gated on it
    # must stay cheap once the database is already current -- a version check
    # is one page read, not a table scan.
    previous_version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.executescript(_SCHEMA_SQL.read_text())
    # Everything schema.sql cannot express: columns on tables that already ship,
    # an index retired, the text index, and the one version-gated data fix. Each
    # is idempotent, so the whole sequence is a no-op on a current database.
    _run_migrations(conn, previous_version)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()


def get_or_create_root(conn: sqlite3.Connection, path: str) -> int:
    """Return the id of the ``roots`` row for ``path``, inserting one if none exists."""
    row = conn.execute("SELECT id FROM roots WHERE path=?", (path,)).fetchone()
    if row:
        return cast(int, row["id"])  # sqlite3.Row.__getitem__ is typed Any
    cur = conn.execute("INSERT INTO roots(path, added_at) VALUES(?, ?)", (path, now_iso()))
    conn.commit()
    # An INSERT that didn't raise always sets lastrowid; typeshed only widens it
    # to int | None because lastrowid is also read after non-INSERT statements.
    return cast(int, cur.lastrowid)


# Tables that key rows to a root directly, so a renumbered root takes them
# along. Deleting a root is different: only `files` needs clearing by hand,
# since the other two cascade and everything else hangs off a file id.
_ROOT_SCOPED_TABLES = ("files", "dedup_runs", "place_clusters")


def _delete_root_files(conn: sqlite3.Connection, root_id: int) -> None:
    """Delete one root's files, and the text-index rows left stranded by that.

    Everything derived from a file cascades away with it -- except the one table
    that cannot. ``doc_chunk_fts`` is a virtual table, so it can carry no foreign
    key; nothing ties it to ``doc_chunks`` but the rowid its writer keeps equal.
    And the cascade would not help even if it could: SQLite fires AFTER DELETE
    triggers on a foreign-key cascade only under ``PRAGMA recursive_triggers``,
    which is off (see ``_migrate_text_index``).

    So the index is cleared here, before the delete that would strand it. This
    and ``services/documents.py``'s chunk writes are the whole of that
    obligation; there are no triggers anywhere to reason about.
    """
    if text_index_present(conn):
        conn.execute(
            """DELETE FROM doc_chunk_fts WHERE rowid IN
                   (SELECT id FROM doc_chunks WHERE file_id IN
                       (SELECT id FROM files WHERE root_id=?))""",
            (root_id,),
        )
    conn.execute("DELETE FROM files WHERE root_id=?", (root_id,))


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
            _delete_root_files(conn, rid)
            conn.execute("DELETE FROM roots WHERE id=?", (rid,))
        if owner is None:
            # A row may still be sitting on the target id under a stale path.
            _delete_root_files(conn, root_id)
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
            _delete_root_files(conn, root_id)
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


def scan_run_start(conn: sqlite3.Connection, root_id: int, roots: Iterable[str]) -> int:
    """Insert an open ``scan_runs`` row (``finished_at`` still NULL) and return its id."""
    cur = conn.execute(
        "INSERT INTO scan_runs(started_at, roots, root_id) VALUES(?,?,?)",
        (now_iso(), json.dumps(list(roots)), root_id),
    )
    conn.commit()
    # An INSERT that didn't raise always sets lastrowid; see get_or_create_root.
    return cast(int, cur.lastrowid)


def scan_run_finish(
    conn: sqlite3.Connection, run_id: int, stats: _ScanStatsLike, files_on_disk: int | None
) -> None:
    """Close out a scan that walked the whole root. Only ever called on the
    normal path — an interrupted scan leaves ``finished_at`` NULL so it is not
    mistaken for full coverage."""
    conn.execute(
        """UPDATE scan_runs SET finished_at=?, files_seen=?, files_new=?,
           files_updated=?, bytes_hashed=?, files_on_disk=?, files_unstable=?
           WHERE id=?""",
        (
            now_iso(),
            stats.seen,
            stats.new,
            stats.updated,
            stats.bytes_hashed,
            files_on_disk,
            stats.unstable,
            run_id,
        ),
    )
    conn.commit()


_LAST_COMPLETED = """SELECT finished_at, files_on_disk, {unstable} AS files_unstable
    FROM scan_runs
    WHERE root_id=? AND finished_at IS NOT NULL AND files_on_disk IS NOT NULL
    ORDER BY id DESC LIMIT 1"""


def last_completed_scan(conn: sqlite3.Connection, root_id: int) -> sqlite3.Row | None:
    """The most recent scan run that walked this root end to end.

    Public because more than one thing needs to know it, and they must not each
    decide it for themselves. The start page once asked its own version of this
    question -- "is the newest run still open" -- which is a different question
    with a different answer: a scan cancelled by closing the archive leaves an
    open row that no later run replaces, because an archive that is already
    covered never queues another scan. The card read that as "still being read"
    and said so forever, about an archive the pipeline had long since finished.
    """
    try:
        row = conn.execute(
            _LAST_COMPLETED.format(unstable="COALESCE(files_unstable,0)"), (root_id,)
        ).fetchone()
    except sqlite3.OperationalError as exc:
        # The start page is the one caller that can meet a database this build
        # has never migrated: it reads every registered archive read-only,
        # without opening it, and opening is what runs init_db. So on the first
        # launch after an upgrade -- for every archive at once -- this column
        # does not exist yet. Older runs pre-date the question anyway, and the
        # answer for them is the column's own default.
        if "files_unstable" not in str(exc):
            raise
        row = conn.execute(_LAST_COMPLETED.format(unstable="0"), (root_id,)).fetchone()
    return cast("sqlite3.Row | None", row)


def scan_settled(conn: sqlite3.Connection, root_id: int, files_on_disk: int | None) -> bool:
    """True when a completed scan already covers exactly what is on disk now.

    ``files_on_disk`` is the current count; comparing it with the one the last
    completed scan saw also catches deletions, which a backlog subtraction
    silently floors at zero.

    A count match is not enough on its own. A run that walked past a file still
    being copied counted it on disk and deliberately did not catalogue it, so
    the counts agree while the archive is genuinely short of one file — and
    since finishing a copy changes no count, nothing would ever ask again.
    ``files_unstable`` is what keeps that run from passing for coverage.
    """
    if files_on_disk is None:
        return False
    row = last_completed_scan(conn, root_id)
    return row is not None and row["files_on_disk"] == files_on_disk and not row["files_unstable"]


def scan_awaiting_settle(conn: sqlite3.Connection, root_id: int, cooldown: float) -> bool:
    """Whether the last completed run left files still arriving, recently enough
    that asking again now would only find them still arriving.

    The scan is the one stage whose backlog is answered by walking the whole
    tree, so "not settled" is an expensive question to keep asking. Without this
    the scheduler would re-walk every tick for the entire length of a large
    copy — which is the same hot loop ``scan_settled`` exists to prevent, just
    reached from the other side.
    """
    row = last_completed_scan(conn, root_id)
    if row is None or not row["files_unstable"]:
        return False
    try:
        finished = datetime.fromisoformat(row["finished_at"])
    except ValueError:
        # An unparseable timestamp must not wedge the stage off: fall through
        # and let the scan run, which is the behaviour without this check.
        return False
    return (datetime.now(UTC) - finished).total_seconds() < cooldown


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


def present_file_count(conn: sqlite3.Connection, root_id: int) -> int:
    """How many files under this root the catalogue already holds.

    Read by the scan runner as the length of the ground a restarted walk has
    to re-cross before it reaches anything new -- see ``Job.recheck_below``.
    Counts what is *present*, since a file marked missing is not going to turn
    up in the walk and would push the mark past where the walk can reach.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM files WHERE root_id=? AND present=1", (root_id,)
    ).fetchone()
    return int(row[0])


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

    Its caller (``pipeline/runners/dedup.py``) writes this on the rebuild's
    connection but in a later transaction than ``exact.run``'s own commit, so
    the marker can lag the grouping it describes if the process dies in that
    narrow window.
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
    # bool(...): sqlite3.Row.__getitem__ is typed Any, but `!=`/`or` on the ints
    # actually stored here always produce a real bool at runtime.
    return bool(row[0] != current_files or row[1] != current_max_id)
