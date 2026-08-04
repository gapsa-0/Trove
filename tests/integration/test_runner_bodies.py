"""Each stage's ``run()`` executed at least once, through the real seam.

``tests/gui/test_runner_dispatch.py`` covers the *handover* -- context,
connection lifetime, lock modes, cancellation -- with fake runners. That is the
part a refactor breaks. It says nothing about the runners themselves, and a
measurement bore that out: planting ``raise RuntimeError`` as the first line of
each ``run()`` left the whole suite green for every stage except ``dedup``.

So these tests do the cheap, blunt thing: build a small archive, hand each
runner a real ``JobContext``, and assert the contract the manager and the GUI
rely on -- that the body runs to completion, sets the progress fields, and
leaves a message the status card can show. They are not a substitute for each
stage's own behavioural tests (``test_dedup``, ``test_scan_settles``,
``test_semantic_video`` and the detect suite all drive the underlying functions
directly); they are the guard against a runner that no longer calls them.

``detect`` and ``semantic`` are deliberately absent: both load ONNX models and
need real decodable media, which is a fixture cost this file cannot carry. Their
underlying passes are covered directly elsewhere, and what remains uncovered for
them is the ten-line wrapper.
"""

from __future__ import annotations

import logging
import sqlite3
import threading

import factories
import pytest
from helpers import needs_scoring

from organize_archive.config import Config
from organize_archive.db import database as db
from organize_archive.pipeline.job import Job, JobContext
from organize_archive.pipeline.runners import (
    enrich as enrich_runner,
)
from organize_archive.pipeline.runners import (
    face_cluster as face_cluster_runner,
)
from organize_archive.pipeline.runners import (
    pet_cluster as pet_cluster_runner,
)
from organize_archive.pipeline.runners import (
    scan as scan_runner,
)
from organize_archive.scan import walker

_ROOT = 1


def _context(cfg: Config, conn: sqlite3.Connection, kind: str, root_path: str | None = None):
    """A real JobContext, as the manager builds one -- minus the thread."""
    job = Job(id=1, kind=kind, root_id=_ROOT, root_path=root_path)
    ctx = JobContext(
        cfg=cfg,
        job=job,
        cancel=threading.Event(),
        conn=conn,
        log=logging.getLogger(f"test.{kind}"),
    )
    return ctx, job


@pytest.fixture
def archive(tmp_path, monkeypatch):
    """A registered archive with real files on disk and a database.

    The bytes are not valid images on purpose (see ``make_archive``): scan and
    enrich both have to cope with a file they cannot decode, which is a real
    condition in this archive, and a test that quietly depended on decoding
    would pass for the wrong reason.
    """
    source = factories.make_archive(
        tmp_path,
        {
            "2022/IMG_20220514_090957.jpg": b"not really a jpeg",
            "2022/IMG_20220514_090958.jpg": b"nor is this one",
            "clips/VID_20220514.mp4": b"not really an mp4",
        },
    )
    conn = factories.make_db(tmp_path)
    conn.execute("DELETE FROM roots")
    conn.execute(
        "INSERT INTO roots(id,path,added_at) VALUES(?,?,?)", (_ROOT, str(source), db.now_iso())
    )
    conn.commit()
    cfg = Config()
    monkeypatch.setattr(cfg, "archive_path", lambda root_id: str(source), raising=False)
    return cfg, conn, str(source)


def test_the_scan_runner_walks_the_root_and_reports_what_it_saw(archive):
    cfg, conn, source = archive
    ctx, job = _context(cfg, conn, "scan", root_path=source)

    scan_runner.run(ctx)

    indexed = conn.execute("SELECT COUNT(*) FROM files WHERE root_id=?", (_ROOT,)).fetchone()[0]
    assert indexed > 0, "the scan stage catalogued nothing"
    assert "files scanned" in job.message
    # The scan run is closed only when every root was walked end to end, so an
    # open run here would mean the body returned early.
    assert (
        conn.execute("SELECT COUNT(*) FROM scan_runs WHERE finished_at IS NULL").fetchone()[0] == 0
    )


def test_the_scan_runner_counts_the_disk_before_it_claims_any_progress(archive, monkeypatch):
    """The walk that establishes the total is minutes on a cold ~150k-file
    tree, and a bar drawn across it is a bar counting a total nothing has
    started consuming. See JobContext.preparing."""
    cfg, conn, source = archive
    ctx, job = _context(cfg, conn, "scan", root_path=source)
    seen = []
    real_count = walker.count_files
    monkeypatch.setattr(
        walker, "count_files", lambda p: seen.append((job.phase, job.current)) or real_count(p)
    )

    scan_runner.run(ctx)

    assert seen == [("preparing", "counting files on disk")]
    assert job.phase == "working", "the bar has to come back for the walk itself"
    assert job.total > 0


def test_the_enrich_runner_processes_the_scanned_files(archive):
    cfg, conn, source = archive
    scan_ctx, _ = _context(cfg, conn, "scan", root_path=source)
    scan_runner.run(scan_ctx)
    ctx, job = _context(cfg, conn, "enrich")

    enrich_runner.run(ctx)

    assert "processed" in job.message
    assert "Takeout-matched" in job.message


def test_the_face_cluster_runner_reclusters_and_reports(archive):
    """Reached from a review undo, not from the scheduler -- so its body is on
    a path no other test walks."""
    cfg, conn, _ = archive
    file_id = factories.add_file(conn, root_id=_ROOT)
    person = factories.add_person(conn, name="Mari")
    factories.add_face(conn, file_id=file_id, person_id=person)
    conn.commit()
    ctx, job = _context(cfg, conn, "face_cluster")

    face_cluster_runner.run(ctx)

    assert job.total == job.done == 1
    assert "people" in job.message and "faces clustered" in job.message


@needs_scoring
def test_the_pet_cluster_runner_reclusters_and_reports(archive):
    cfg, conn, _ = archive
    file_id = factories.add_file(conn, root_id=_ROOT)
    factories.add_pet(conn, name="Kira")
    factories.add_animal_detection(conn, file_id=file_id)
    conn.commit()
    ctx, job = _context(cfg, conn, "pet_cluster")

    pet_cluster_runner.run(ctx)

    assert job.total == job.done == 1
    assert "pets" in job.message and "detections clustered" in job.message
