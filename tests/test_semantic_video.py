"""Semantic video indexing: frame sampling, frame averaging, permanent skips.

A video is never embedded as a whole file. ``media_part`` samples a handful of
frames spread across the clip, each frame is embedded on its own, and the unit
vectors are averaged back into one -- so a clip lands in the same space as a
photo and is comparable under the same cosine threshold, instead of being
represented by whichever single frame happened to be first.

These cover that contract plus the failure taxonomy around it: a video ffmpeg
cannot decode is a *permanent skip* (nothing will ever change by retrying),
while a genuinely unexpected failure stays a retryable error, and one bad file
costs one file rather than the whole pass.
"""

from __future__ import annotations

import threading

import numpy as np
import pytest

from organize_archive.config import Config
from organize_archive.db import database as db
from organize_archive.embeddings import backend as eb
from organize_archive.gui import jobs as jobs_mod
from organize_archive.gui import semantic, thumbs


def _archive_db(tmp_path, sha256="abc", media_type="video"):
    db_path = tmp_path / "archive.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/x','2026-01-01')")
    conn.execute(
        """INSERT INTO files
           (id,root_id,rel_path,size,mtime,media_type,sha256,present,hidden,
            first_seen,last_seen)
           VALUES(1,1,'clip.mp4',10,0,?,?,1,0,'2026-01-01','2026-01-01')""",
        (media_type, sha256),
    )
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# media_part(): sampled frames, as file paths
# ---------------------------------------------------------------------------


def test_video_media_part_returns_sampled_frame_paths(tmp_path, monkeypatch):
    frames = []
    for i in range(3):
        fp = tmp_path / f"frame{i}.jpg"
        fp.write_bytes(b"jpeg")
        frames.append(fp)
    seen = {}

    def fake_frames(cache_dir, fid, src, offsets, size=1024, sha256=None, rotate=0):
        seen["offsets"] = offsets
        seen["size"] = size
        return frames

    monkeypatch.setattr(thumbs, "video_frames_for", fake_frames)

    part, kind, reason = semantic.media_part(
        Config(),
        tmp_path / "clip.mp4",
        "mp4",
        "video",
        str(tmp_path / "cache"),
        rotate=0,
        duration_s=12.0,
    )

    assert reason is None
    assert kind == "video_frames"
    assert part == frames
    # Spread across the clip, and pulled in from both ends where title cards and
    # black frames live.
    assert len(seen["offsets"]) == 3
    assert seen["offsets"][0] > "00:00:00.000"


def test_image_media_part_returns_one_cached_thumbnail(tmp_path, monkeypatch):
    thumb = tmp_path / "thumb.jpg"
    thumb.write_bytes(b"jpeg")
    monkeypatch.setattr(
        thumbs, "thumb_for", lambda cache_dir, fid, src, size=320, sha256=None, rotate=0: thumb
    )

    part, kind, reason = semantic.media_part(
        Config(), tmp_path / "photo.jpg", "jpg", "image", str(tmp_path / "cache")
    )

    assert (part, kind, reason) == ([thumb], "thumbnail", None)


def test_no_extractable_frames_is_a_clean_permanent_skip(tmp_path, monkeypatch):
    """No ffmpeg (or a container it can't read) must land as a permanent skip,
    same family as "unsupported format" -- never as a retryable error."""
    monkeypatch.setattr(
        thumbs,
        "video_frames_for",
        lambda cache_dir, fid, src, offsets, size=1024, sha256=None, rotate=0: [],
    )

    part, kind, reason = semantic.media_part(
        Config(),
        tmp_path / "clip.mp4",
        "mp4",
        "video",
        str(tmp_path / "cache"),
        rotate=0,
        duration_s=None,
    )

    assert part is None and kind is None
    assert reason is not None
    assert semantic._is_permanent_skip(reason)


def test_undecodable_image_is_a_clean_permanent_skip(tmp_path, monkeypatch):
    """The thumbnailer is the only decoder, so what it refuses cannot be indexed."""
    monkeypatch.setattr(
        thumbs, "thumb_for", lambda cache_dir, fid, src, size=320, sha256=None, rotate=0: None
    )

    part, _kind, reason = semantic.media_part(
        Config(), tmp_path / "photo.xyz", "xyz", "image", str(tmp_path / "cache")
    )

    assert part is None
    assert semantic._is_permanent_skip(reason)


# ---------------------------------------------------------------------------
# Frame averaging
# ---------------------------------------------------------------------------


class _FakeVisionBackend(eb.SiglipBackend):
    """Only the model call is faked; normalisation and averaging stay real."""

    def __init__(self, vectors):
        self._vectors = [np.asarray(v, dtype=np.float32) for v in vectors]
        self.calls = 0

    def embed_images(self, items):
        items = list(items)
        self.calls += 1
        if not items:
            return np.zeros((0, 3), dtype=np.float32)
        return self._normalize(np.stack(self._vectors[: len(items)]))


def test_video_vector_is_the_renormalised_mean_of_its_frames():
    backend = _FakeVisionBackend([[3.0, 0.0, 0.0], [0.0, 4.0, 0.0], [0.0, 0.0, 5.0]])

    vector = backend.embed_frames_mean(["a", "b", "c"])

    # Each frame contributes as a *unit* vector, so the differing raw magnitudes
    # above must not weight one frame over another.
    assert vector == pytest.approx(np.full(3, 1 / np.sqrt(3)), abs=1e-6)
    assert float(np.linalg.norm(vector)) == pytest.approx(1.0, abs=1e-6)


def test_a_single_readable_frame_still_produces_a_unit_vector():
    backend = _FakeVisionBackend([[0.0, 2.0, 0.0]])

    vector = backend.embed_frames_mean(["only-frame"])

    assert vector == pytest.approx(np.array([0.0, 1.0, 0.0]), abs=1e-6)


def test_no_frames_at_all_yields_no_vector():
    assert _FakeVisionBackend([]).embed_frames_mean([]) is None


# ---------------------------------------------------------------------------
# save_outcome(): the skip/error split
# ---------------------------------------------------------------------------


def test_an_undecodable_source_is_recorded_as_skipped(tmp_path):
    db_path = _archive_db(tmp_path)
    conn = db.connect(db_path)
    row = {"id": 1, "sha256": "abc"}

    semantic.save_outcome(
        conn,
        Config(),
        row,
        None,
        "video_frames",
        "unsupported video: could not extract any frames (ffmpeg missing or unreadable video)",
    )
    conn.commit()

    status = conn.execute("SELECT status FROM semantic_embeddings WHERE file_id=1").fetchone()[0]
    assert status == "skipped"


def test_an_ordinary_failure_still_records_as_error(tmp_path):
    """The classifier must stay narrow: a real (transient/unexpected) failure
    is still "error", not swallowed into "skipped"."""
    db_path = _archive_db(tmp_path)
    conn = db.connect(db_path)
    row = {"id": 1, "sha256": "abc"}

    semantic.save_outcome(
        conn, Config(), row, None, "video_frames", "OSError: cannot identify image file"
    )
    conn.commit()

    status = conn.execute("SELECT status FROM semantic_embeddings WHERE file_id=1").fetchone()[0]
    assert status == "error"


# ---------------------------------------------------------------------------
# JobManager._semantic_pass(): one bad file costs one file
# ---------------------------------------------------------------------------


def _job_manager(tmp_path, monkeypatch):
    # Everything stays under tmp_path: archive_db_path/archive_cache_dir
    # normally resolve under the user's real ~/.local/share/organize_archive,
    # which must never be touched by a test.
    monkeypatch.setattr(Config, "archive_db_path", lambda self, aid: str(tmp_path / "archive.db"))
    monkeypatch.setattr(Config, "archive_cache_dir", lambda self, aid: str(tmp_path / "cache"))
    return jobs_mod.JobManager(Config())


def test_a_failing_file_is_recorded_once_and_never_retried(tmp_path, monkeypatch):
    """Local inference has no batch to isolate from.

    The old code sent batches over HTTP, so a failure had to be re-issued per
    item to find the culprit. Here a file either decodes or it does not: it must
    be embedded exactly once, and its failure recorded without a second attempt.
    """
    _archive_db(tmp_path)
    jm = _job_manager(tmp_path, monkeypatch)
    try:
        row = {
            "id": 1,
            "rel_path": "clip.mp4",
            "ext": "mp4",
            "media_type": "video",
            "sha256": "abc",
            "root_path": "/x",
            "rotate_deg": 0,
            "duration_s": 5.0,
        }
        monkeypatch.setattr(semantic, "pending_rows", lambda conn, root_id, force=False: [row])
        monkeypatch.setattr(semantic, "work_counts", lambda conn, root_id, force=False: (1, 0))
        monkeypatch.setattr(
            semantic,
            "media_part",
            lambda cfg, path, ext, media_type, cache_dir, rotate, duration_s: (
                [tmp_path / "frame.jpg"],
                "video_frames",
                None,
            ),
        )
        calls = []

        def fake_embed_part(cfg, part, kind):
            calls.append((list(part), kind))
            raise OSError("broken frame")

        monkeypatch.setattr(semantic, "embed_part", fake_embed_part)

        job = jobs_mod.Job(id=1, kind="semantic", root_id=1, root_path="/x")
        indexed, skipped, failed, total = jm._semantic_pass(job, threading.Event(), force=False)

        assert len(calls) == 1
        assert (indexed, skipped, failed, total) == (0, 0, 1, 1)

        conn = db.connect(tmp_path / "archive.db")
        stored = conn.execute(
            "SELECT status, error FROM semantic_embeddings WHERE file_id=1"
        ).fetchone()
        assert stored["status"] == "error"
        assert "broken frame" in stored["error"]
    finally:
        jm.shutdown(timeout=2.0)


def test_one_bad_file_does_not_take_its_neighbours_down(tmp_path, monkeypatch):
    _archive_db(tmp_path, sha256="abc")
    conn = db.connect(tmp_path / "archive.db")
    conn.execute(
        """INSERT INTO files
           (id,root_id,rel_path,size,mtime,media_type,sha256,present,hidden,
            first_seen,last_seen)
           VALUES(2,1,'photo.jpg',10,0,'image','def',1,0,'2026-01-01','2026-01-01')"""
    )
    conn.commit()
    conn.close()

    jm = _job_manager(tmp_path, monkeypatch)
    try:
        rows = [
            {
                "id": 1,
                "rel_path": "clip.mp4",
                "ext": "mp4",
                "media_type": "video",
                "sha256": "abc",
                "root_path": "/x",
                "rotate_deg": 0,
                "duration_s": 5.0,
            },
            {
                "id": 2,
                "rel_path": "photo.jpg",
                "ext": "jpg",
                "media_type": "image",
                "sha256": "def",
                "root_path": "/x",
                "rotate_deg": 0,
                "duration_s": None,
            },
        ]
        monkeypatch.setattr(semantic, "pending_rows", lambda conn, root_id, force=False: rows)
        monkeypatch.setattr(semantic, "work_counts", lambda conn, root_id, force=False: (2, 0))
        monkeypatch.setattr(
            semantic,
            "media_part",
            lambda cfg, path, ext, media_type, cache_dir, rotate, duration_s: (
                [tmp_path / "x.jpg"],
                "video_frames" if media_type == "video" else "thumbnail",
                None,
            ),
        )

        def fake_embed_part(cfg, part, kind):
            if kind == "video_frames":
                raise OSError("broken frame")
            return [0.1, 0.2]

        monkeypatch.setattr(semantic, "embed_part", fake_embed_part)

        job = jobs_mod.Job(id=1, kind="semantic", root_id=1, root_path="/x")
        indexed, skipped, failed, total = jm._semantic_pass(job, threading.Event(), force=False)

        assert (indexed, skipped, failed, total) == (1, 0, 1, 2)

        conn = db.connect(tmp_path / "archive.db")
        rows_out = {
            r["file_id"]: r["status"]
            for r in conn.execute("SELECT file_id, status FROM semantic_embeddings")
        }
        assert rows_out == {1: "error", 2: "indexed"}
    finally:
        jm.shutdown(timeout=2.0)
