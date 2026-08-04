"""Detect's commit cadence: time-budgeted, not once-per-batch.

Mirrors metadata/enrich.py's ~2s commit budget (enrich.py:220). Before this,
extract() opened its write transaction on the first DELETE of a batch and
committed only after every image in it (up to ``batch_size``, default 32) --
each a full SCRFD+YOLOX decode plus up to three extra YOLOX passes -- which
could hold the single writer well past other connections' 30s busy_timeout
(the parallel semantic worker's own small writes, HTTP handler writes),
surfacing as a spurious "database is locked" instead of a lock that just
waits out a short overlap. Committing on a wall-clock budget bounds that
window to about 2 seconds regardless of batch size.
"""

from __future__ import annotations

import sqlite3
import time as time_mod

import pytest

from trove.config import Config
from trove.db import database as db
from trove.detect import extract as dx
from trove.faces import backend as face_backend

np = pytest.importorskip("numpy")


class _NullPetBackend:
    """No animals, no humans -- keeps every row on the cheapest path so the
    test is only exercising the commit cadence, not the cross-check logic
    already covered by test_detect_human_veto.py."""

    def detect_with_humans(self, _img):
        return [], []


class _NullFaceBackend:
    def detect_report(self, _img, _scale=1.0):
        return face_backend.DetectionReport()


class _CountingConnection(sqlite3.Connection):
    """A real sqlite3.Connection that counts its own commit() calls.

    sqlite3.Connection is an immutable extension type -- neither an instance
    attribute nor a class attribute can be monkeypatched onto it after the
    fact (both raise), so counting requires a subclass supplied as the
    ``factory`` at connect time instead.
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.commit_calls = 0

    def commit(self):
        self.commit_calls += 1
        return super().commit()


def _connect_counting(path) -> _CountingConnection:
    # Mirrors db.connect()'s pragma setup (db/database.py) so behavior under
    # test matches production, just with commit() instrumented.
    conn = sqlite3.connect(str(path), timeout=30.0, factory=_CountingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _catalog(tmp_path, n: int) -> _CountingConnection:
    root = tmp_path / "photos"
    root.mkdir()
    conn = _connect_counting(tmp_path / "archive.db")
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,?,'2026-01-01')", (str(root),))
    for i in range(1, n + 1):
        (root / f"{i}.jpg").write_bytes(b"fake")
        conn.execute(
            """INSERT INTO files
               (id,root_id,rel_path,size,mtime,media_type,sha256,
                first_seen,last_seen)
               VALUES(?,1,?,4,0,'image',?,'2026-01-01','2026-01-01')""",
            (i, f"{i}.jpg", f"sha{i}"),
        )
    conn.commit()
    conn.commit_calls = 0  # only count commits made during extract() itself
    return conn


def _run_with_fake_clock(conn, cfg, step: float, monkeypatch):
    monkeypatch.setattr(dx, "available", lambda: True)
    monkeypatch.setattr(dx, "load_bgr", lambda _p, _s: (np.zeros((10, 10, 3), "uint8"), 1.0))
    clock = [0.0]

    def fake_monotonic():
        clock[0] += step
        return clock[0]

    monkeypatch.setattr(time_mod, "monotonic", fake_monotonic)
    dx.extract(conn, cfg, face_be=_NullFaceBackend(), pet_be=_NullPetBackend())


def test_commits_on_the_time_budget_not_just_at_batch_end(tmp_path, monkeypatch):
    """A clock that always reads past the 2s budget must force a commit after
    every image, not just the one unconditional commit at the end of the
    batch -- otherwise a kill mid-batch loses everything since the last batch
    boundary, exactly the bug this replaces."""
    conn = _catalog(tmp_path, n=3)

    _run_with_fake_clock(conn, Config(), step=100.0, monkeypatch=monkeypatch)

    # One budget-triggered commit per row (3) plus the batch's own final,
    # unconditional commit.
    assert conn.commit_calls == 4
    conn.close()


def test_does_not_commit_before_the_budget_elapses(tmp_path, monkeypatch):
    """A clock that never advances must behave like the old code: exactly one
    commit for the whole batch, not one per image."""
    conn = _catalog(tmp_path, n=3)

    _run_with_fake_clock(conn, Config(), step=0.0, monkeypatch=monkeypatch)

    assert conn.commit_calls == 1
    conn.close()
