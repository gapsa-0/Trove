"""A rebuild must not re-search an archive that did not change.

The bug these pin, measured on this project's own 97,000-file archive: deleting
five files -- all of them singletons, belonging to no duplicate group at all --
started a rebuild that spent 1,165 seconds comparing every fingerprint against
every other one, ignored the cancellation it was sent five minutes in because
the loop doing the comparing had no checkpoint, and was killed at shutdown
without recording anything, so the next launch began the same 1,165 seconds
again.

Every assertion below is about work *not* done. The grouping itself is still
rebuilt wholesale on every run, and `test_an_incremental_run_groups_exactly_like_
a_full_one` is the guard that keeps "cheaper" from quietly meaning "different".
"""

from __future__ import annotations

import sqlite3
import threading

import factories
import pytest

from trove.config import Config
from trove.db import database as db
from trove.dedup import bands, exact
from trove.pipeline.job import Job, JobProgress


@pytest.fixture
def searches(monkeypatch):
    """Counts the fingerprints looked up in the index, per run.

    The honest measure of the cost this change is about: not how long a run
    took, but how many files it compared against the archive.
    """
    seen: list[int] = []
    original = bands.BandIndex.within

    def counting(self, value):
        seen.append(value)
        return original(self, value)

    monkeypatch.setattr(bands.BandIndex, "within", counting)
    return seen


def _fingerprints(monkeypatch, table: dict[str, int]) -> None:
    """Stand in for the decode pass, serving fingerprints from the test's table.

    Keyed by ``rel_path`` so a test can re-point a path at a different picture
    the way a re-saved file does, and so a file the table does not mention (a
    video, an undecodable image) is simply absent from the result.
    """

    def compute(conn, progress=None, root_id=None):
        rows = conn.execute(
            """SELECT id, rel_path FROM files
                WHERE present=1 AND media_type='image' AND sha256 IS NOT NULL"""
        ).fetchall()
        return {r["id"]: table[r["rel_path"]] for r in rows if r["rel_path"] in table}

    monkeypatch.setattr(exact.fingerprints, "compute", compute)


def _grouping(conn):
    """The grouping as a comparable value: who is hidden, and who is with whom."""
    members = conn.execute(
        """SELECT m.file_id, m.role, g.method, g.canonical_file_id
             FROM dup_members m JOIN dup_groups g ON g.id=m.group_id
            ORDER BY m.file_id"""
    ).fetchall()
    hidden = conn.execute("SELECT id, hidden FROM files ORDER BY id").fetchall()
    return [tuple(r) for r in members], [tuple(r) for r in hidden]


# Two fingerprints 2 bits apart (the same photo re-saved) and one unrelated.
NEAR_A = 0b0000
NEAR_B = 0b0011
FAR = 2**64 - 1


def test_a_rerun_over_an_unchanged_archive_searches_nothing(searches):
    conn, _ = factories.make_memory_db()
    for i in range(1, 4):
        factories.add_file(conn, rel_path=f"{i}.jpg", sha256=chr(96 + i) * 64, size=10)
    conn.commit()

    with pytest.MonkeyPatch.context() as mp:
        _fingerprints(mp, {"1.jpg": NEAR_A, "2.jpg": NEAR_B, "3.jpg": FAR})
        exact.run(conn, Config())
        first = len(searches)
        searches.clear()

        exact.run(conn, Config())

    assert first == 3, "the first run has to search every fingerprint once"
    assert searches == [], "an unchanged archive must not be searched again"


def test_deleting_a_file_searches_nothing(searches):
    """The reported case: files vanish from disk, the scan marks them absent,
    and the rebuild that follows has no fingerprint to look up."""
    conn, _ = factories.make_memory_db()
    for i in range(1, 4):
        factories.add_file(conn, rel_path=f"{i}.jpg", sha256=chr(96 + i) * 64, size=10)
    conn.commit()

    with pytest.MonkeyPatch.context() as mp:
        _fingerprints(mp, {"1.jpg": NEAR_A, "2.jpg": NEAR_B, "3.jpg": FAR})
        exact.run(conn, Config())
        searches.clear()

        conn.execute("UPDATE files SET present=0 WHERE rel_path='3.jpg'")
        conn.commit()
        exact.run(conn, Config())

    assert searches == []
    # The pair that survived the deletion is still a group, without being re-found.
    assert conn.execute("SELECT COUNT(*) FROM dup_groups").fetchone()[0] == 1


def test_adding_a_file_searches_only_that_file(searches):
    conn, _ = factories.make_memory_db()
    for i in range(1, 4):
        factories.add_file(conn, rel_path=f"{i}.jpg", sha256=chr(96 + i) * 64, size=10)
    conn.commit()

    with pytest.MonkeyPatch.context() as mp:
        table = {"1.jpg": NEAR_A, "2.jpg": NEAR_B, "3.jpg": FAR}
        _fingerprints(mp, table)
        exact.run(conn, Config())
        searches.clear()

        new_id = factories.add_file(conn, rel_path="4.jpg", sha256="d" * 64, size=10)
        conn.commit()
        table["4.jpg"] = NEAR_A  # another copy of the same photo
        exact.run(conn, Config())

    assert len(searches) == 1, "only the new file is owed a search"
    # ...and it still joined the group the two older copies were already in.
    group = conn.execute("SELECT COUNT(*) FROM dup_members WHERE file_id=?", (new_id,)).fetchone()[
        0
    ]
    assert group == 1
    assert conn.execute("SELECT member_count FROM dup_groups").fetchone()[0] == 3


def test_a_file_whose_content_changed_is_searched_again_and_loses_its_old_pairs(searches):
    """A stored pair describes two files' *content*. Re-save one of them and the
    pair is a statement about a picture that is no longer there."""
    conn, _ = factories.make_memory_db()
    factories.add_file(conn, rel_path="1.jpg", sha256="a" * 64, size=10)
    factories.add_file(conn, rel_path="2.jpg", sha256="b" * 64, size=10)
    conn.commit()

    with pytest.MonkeyPatch.context() as mp:
        table = {"1.jpg": NEAR_A, "2.jpg": NEAR_B}
        _fingerprints(mp, table)
        exact.run(conn, Config())
        assert conn.execute("SELECT COUNT(*) FROM dup_edges").fetchone()[0] == 1
        searches.clear()

        # 2.jpg is replaced by an unrelated picture: new bytes, new fingerprint.
        conn.execute("UPDATE files SET sha256=? WHERE rel_path='2.jpg'", ("c" * 64,))
        conn.commit()
        table["2.jpg"] = FAR
        exact.run(conn, Config())

    assert len(searches) == 1
    assert conn.execute("SELECT COUNT(*) FROM dup_edges").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM dup_groups").fetchone()[0] == 0


def test_raising_the_threshold_searches_the_archive_again(searches):
    """Stored pairs are only true for the threshold they were found under, so
    changing it has to invalidate all of them rather than silently keeping a
    grouping the current setting would not produce."""
    conn, _ = factories.make_memory_db()
    factories.add_file(conn, rel_path="1.jpg", sha256="a" * 64, size=10)
    factories.add_file(conn, rel_path="2.jpg", sha256="b" * 64, size=10)
    conn.commit()

    with pytest.MonkeyPatch.context() as mp:
        # 8 bits apart: outside the default threshold of 6, inside a raised one.
        _fingerprints(mp, {"1.jpg": 0, "2.jpg": 0b11111111})
        exact.run(conn, Config())
        assert conn.execute("SELECT COUNT(*) FROM dup_groups").fetchone()[0] == 0
        searches.clear()

        exact.run(conn, Config(phash_hamming_threshold=10))

    assert len(searches) == 2
    assert conn.execute("SELECT COUNT(*) FROM dup_groups").fetchone()[0] == 1


def test_an_incremental_run_groups_exactly_like_a_full_one(searches):
    """The property that makes all of the above safe.

    Two archives are built to the same final state -- one by adding files a few
    at a time and rebuilding after each, the other in a single run over the
    finished set -- and their groupings have to be identical, down to which copy
    is canonical and which rows are hidden.
    """
    photos = [
        ("a.jpg", "a", NEAR_A),
        ("b.jpg", "b", NEAR_B),
        ("c.jpg", "c", FAR),
        ("d.jpg", "d", NEAR_A | 0b100),  # chains onto a.jpg and b.jpg
        ("e.jpg", "e", FAR ^ 0b1),  # pairs with c.jpg
        ("f.jpg", "f", 0xF0F0F0F0F0F0F0F0),  # alone
    ]

    with pytest.MonkeyPatch.context() as mp:
        incremental, _ = factories.make_memory_db()
        table: dict[str, int] = {}
        _fingerprints(mp, table)
        for rel_path, sha, value in photos:
            factories.add_file(incremental, rel_path=rel_path, sha256=sha * 64, size=10)
            incremental.commit()
            table[rel_path] = value
            exact.run(incremental, Config())
        incremental_searches = len(searches)
        searches.clear()

        full, _ = factories.make_memory_db()
        for rel_path, sha, _value in photos:
            factories.add_file(full, rel_path=rel_path, sha256=sha * 64, size=10)
        full.commit()
        exact.run(full, Config())

    assert _grouping(incremental) == _grouping(full)
    # Six rebuilds searched each file exactly once between them, the same total
    # the single run over the finished archive paid.
    assert incremental_searches == len(photos) == len(searches)


def test_a_cancelled_job_stops_searching_instead_of_finishing_the_pass(searches):
    """The rebuild that would not stop.

    The loop that grew with the archive used to run to completion with no
    checkpoint in it, so `shutdown` logged "cancelling 1 running job(s):
    dedup" and then waited out the whole pass -- with the progress bar frozen
    at the end of the previous phase, which is what "it keeps running but
    doesn't work" looked like from the outside.

    Asserting that it *stopped early* rather than merely that it raised: the
    old code raised too, from the progress call after the search, having done
    every bit of the work it was asked to abandon.
    """
    conn, _ = factories.make_memory_db()
    table = {}
    for i in range(1, 1001):
        rel_path = f"{i}.jpg"
        factories.add_file(conn, rel_path=rel_path, sha256=f"{i:064x}", size=10)
        table[rel_path] = i * 0x1111
    conn.commit()

    cancel = threading.Event()
    cancel.set()
    progress = JobProgress(Job(id=1, kind="dedup", root_id=1, root_path="/x"), cancel)

    with pytest.MonkeyPatch.context() as mp:
        _fingerprints(mp, table)
        with pytest.raises(KeyboardInterrupt):
            exact.run(conn, Config(), progress=progress)

    assert len(searches) <= 100, "cancellation must be answered from inside the search loop"
    # Cancelled before it could publish anything, and -- because the grouping is
    # never cleared until the search is done -- the archive is left untouched
    # rather than half-regrouped.
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM dup_groups").fetchone()[0] == 0


def test_clearing_many_groups_does_not_exhaust_sqlite_bind_variables():
    """`clear` used to bind one parameter per group, so an archive with more
    duplicate groups than the connection allows variables could not be
    regrouped at all -- a failure landing only on the largest archives, which
    are the ones least able to afford it.

    The ceiling is a build-time constant (SQLITE_MAX_VARIABLE_NUMBER, 32,766 by
    default since SQLite 3.32 but 250,000 on the interpreter this suite happens
    to run on), so the test lowers it on its own connection rather than
    building an archive big enough to reach whichever value is in force. That
    keeps it a test of the shape of the SQL, which is the actual defect.
    """
    conn = db.connect(":memory:")
    conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)  # SQLite's pre-3.32 default
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id, path, added_at) VALUES(1, '/x', '2026-01-01')")
    conn.executemany(
        """INSERT INTO files(id, root_id, rel_path, size, mtime, media_type, sha256,
                             first_seen, last_seen, present, hidden)
           VALUES(?, 1, ?, 10, 0, 'image', ?, '2026-01-01', '2026-01-01', 1, 0)""",
        # Consecutive pairs share a SHA, so this is exactly 2,000 exact groups.
        [(i, f"{i}.jpg", f"{(i + 1) // 2:064x}") for i in range(1, 4_001)],
    )
    conn.commit()

    assert exact.run(conn, root_id=1).groups == 2_000
    assert exact.run(conn, root_id=1).groups == 2_000  # the clear-and-rebuild path
