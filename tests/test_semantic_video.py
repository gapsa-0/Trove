"""Semantic video indexing: frame-sampled inputs instead of raw video bytes.

Base64-encoding a whole video file busts Voyage's 32K-token context window on
anything but a tiny clip (root cause C in the background-pipeline-reliability
plan): a 5MB mp4 already fails with "exceed the model's context window of
32000 tokens", identically forever. Worse, jobs.py used to force every video
into its own single-item group, so the failed batch's per-item isolation
retry then re-issued the *exact same* doomed request a second time.

These cover the fix: media_part() sends a handful of sampled frames as one
multi-image input instead of raw bytes; a group that was already down to one
item is recorded directly rather than retried (it can only repeat the same
failure); and an oversize/context-window 400 is a permanent "skipped", not a
retryable "error".
"""

from __future__ import annotations

import base64
import threading

from organize_archive.config import Config
from organize_archive.db import database as db
from organize_archive.gui import jobs as jobs_mod, semantic, thumbs


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
        (media_type, sha256))
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# media_part(): frames, not raw bytes
# ---------------------------------------------------------------------------

def test_video_media_part_sends_sampled_frames_not_raw_bytes(tmp_path, monkeypatch):
    frames = []
    for i, payload in enumerate((b"frame-a", b"frame-b", b"frame-c")):
        fp = tmp_path / f"frame{i}.jpg"
        fp.write_bytes(payload)
        frames.append(fp)
    monkeypatch.setattr(
        thumbs, "video_frames_for",
        lambda cache_dir, fid, src, offsets, size=1024, sha256=None, rotate=0: frames)

    part, kind, reason = semantic.media_part(
        Config(), tmp_path / "clip.mp4", "mp4", "video", str(tmp_path / "cache"),
        rotate=0, duration_s=12.0)

    assert reason is None
    assert kind == "video_frames"          # not the old "video" (video_base64)
    contents = part["content"]
    assert len(contents) == 3              # one Voyage input, several images
    assert all(c["type"] == "image_base64" for c in contents)
    assert all("video_base64" not in c for c in contents)
    decoded = [base64.b64decode(c["image_base64"].split(",", 1)[1]) for c in contents]
    assert decoded == [b"frame-a", b"frame-b", b"frame-c"]


def test_no_extractable_frames_is_a_clean_permanent_skip(tmp_path, monkeypatch):
    """No ffmpeg (or a container it can't read) must land as a permanent skip,
    same family as "unsupported format" -- never as a retryable error."""
    monkeypatch.setattr(
        thumbs, "video_frames_for",
        lambda cache_dir, fid, src, offsets, size=1024, sha256=None, rotate=0: [])

    part, kind, reason = semantic.media_part(
        Config(), tmp_path / "clip.mp4", "mp4", "video", str(tmp_path / "cache"),
        rotate=0, duration_s=None)

    assert part is None and kind is None
    assert reason is not None
    assert semantic._is_permanent_skip(reason)


# ---------------------------------------------------------------------------
# save_outcome(): oversize/context-window 400 -> skipped, not error
# ---------------------------------------------------------------------------

def test_context_window_400_is_recorded_as_skipped(tmp_path):
    db_path = _archive_db(tmp_path)
    conn = db.connect(db_path)
    cfg = Config()
    row = {"id": 1, "sha256": "abc"}
    voyage_400 = (
        "Voyage returned HTTP 400: {\"detail\":\"the inputs at indices [0] "
        "exceed the model's context window of 32000 tokens\"}")

    semantic.save_outcome(conn, cfg, row, None, "video_frames", voyage_400)
    conn.commit()

    status = conn.execute(
        "SELECT status FROM semantic_embeddings WHERE file_id=1").fetchone()[0]
    assert status == "skipped"


def test_an_ordinary_failure_still_records_as_error(tmp_path):
    """The classifier must stay narrow: a real (transient/unexpected) failure
    is still "error", not swallowed into "skipped"."""
    db_path = _archive_db(tmp_path)
    conn = db.connect(db_path)
    cfg = Config()
    row = {"id": 1, "sha256": "abc"}

    semantic.save_outcome(conn, cfg, row, None, "video_frames", "Could not reach Voyage: timed out")
    conn.commit()

    status = conn.execute(
        "SELECT status FROM semantic_embeddings WHERE file_id=1").fetchone()[0]
    assert status == "error"


# ---------------------------------------------------------------------------
# JobManager._semantic_pass(): a lone group is never retried
# ---------------------------------------------------------------------------

def _job_manager(tmp_path, monkeypatch):
    # Everything stays under tmp_path: archive_db_path/archive_cache_dir
    # normally resolve under the user's real ~/.local/share/organize_archive,
    # which must never be touched by a test.
    monkeypatch.setattr(Config, "archive_db_path", lambda self, aid: str(tmp_path / "archive.db"))
    monkeypatch.setattr(Config, "archive_cache_dir", lambda self, aid: str(tmp_path / "cache"))
    jm = jobs_mod.JobManager(Config())
    return jm


def test_single_item_group_failure_is_recorded_without_a_retry(tmp_path, monkeypatch):
    _archive_db(tmp_path)
    jm = _job_manager(tmp_path, monkeypatch)
    try:
        row = {"id": 1, "rel_path": "clip.mp4", "ext": "mp4", "media_type": "video",
              "sha256": "abc", "root_path": "/x", "rotate_deg": 0, "duration_s": 5.0}
        monkeypatch.setattr(semantic, "pending_rows", lambda conn, root_id, force=False: [row])
        monkeypatch.setattr(semantic, "work_counts", lambda conn, root_id, force=False: (1, 0))
        monkeypatch.setattr(
            semantic, "media_part",
            lambda cfg, path, ext, media_type, cache_dir, rotate, duration_s:
                ({"content": [{"type": "image_base64", "image_base64": "x"}]},
                 "video_frames", None))
        calls = []

        def fake_embed_parts(cfg, parts):
            calls.append(list(parts))
            raise semantic.VoyageEmbeddingError(
                "Voyage returned HTTP 400: exceed the model's context window of 32000 tokens")

        monkeypatch.setattr(semantic, "embed_parts", fake_embed_parts)

        job = jobs_mod.Job(id=1, kind="semantic", root_id=1, root_path="/x")
        cancel = threading.Event()

        indexed, skipped, failed, total = jm._semantic_pass(job, cancel, force=False)

        # The lone group's request failed once -- and was never re-issued in
        # isolation, since it already *was* the isolated request.
        assert len(calls) == 1
        assert (indexed, skipped, failed, total) == (0, 0, 1, 1)

        conn = db.connect(tmp_path / "archive.db")
        row_out = conn.execute(
            "SELECT status, input_kind FROM semantic_embeddings WHERE file_id=1"
        ).fetchone()
        # Context-window 400 on the one-and-only attempt: permanent skip.
        assert row_out["status"] == "skipped"
        assert row_out["input_kind"] == "video_frames"
    finally:
        jm.shutdown(timeout=2.0)


def test_multi_item_group_still_isolates_the_bad_item(tmp_path, monkeypatch):
    """Guard against a regression in the surrounding retry logic: a batch of
    several items that fails as a whole must still fall back to individual
    calls, so one bad source doesn't discard its good neighbours."""
    _archive_db(tmp_path, sha256="abc")
    conn = db.connect(tmp_path / "archive.db")
    conn.execute(
        """INSERT INTO files
           (id,root_id,rel_path,size,mtime,media_type,sha256,present,hidden,
            first_seen,last_seen)
           VALUES(2,1,'photo.jpg',10,0,'image','def',1,0,'2026-01-01','2026-01-01')""")
    conn.commit()
    conn.close()

    jm = _job_manager(tmp_path, monkeypatch)
    try:
        rows = [
            {"id": 1, "rel_path": "clip.mp4", "ext": "mp4", "media_type": "video",
             "sha256": "abc", "root_path": "/x", "rotate_deg": 0, "duration_s": 5.0},
            {"id": 2, "rel_path": "photo.jpg", "ext": "jpg", "media_type": "image",
             "sha256": "def", "root_path": "/x", "rotate_deg": 0, "duration_s": None},
        ]
        monkeypatch.setattr(semantic, "pending_rows", lambda conn, root_id, force=False: rows)
        monkeypatch.setattr(semantic, "work_counts", lambda conn, root_id, force=False: (2, 0))
        monkeypatch.setattr(
            semantic, "media_part",
            lambda cfg, path, ext, media_type, cache_dir, rotate, duration_s:
                ({"content": [{"type": "image_base64", "image_base64": "x"}]},
                 "video_frames" if media_type == "video" else "image", None))

        batch_calls, solo_calls = [], []

        def fake_embed_parts(cfg, parts):
            if len(parts) > 1:
                batch_calls.append(list(parts))
                raise semantic.VoyageEmbeddingError("boom")
            solo_calls.append(list(parts))
            return [[0.1, 0.2]]

        monkeypatch.setattr(semantic, "embed_parts", fake_embed_parts)

        job = jobs_mod.Job(id=1, kind="semantic", root_id=1, root_path="/x")
        cancel = threading.Event()

        indexed, skipped, failed, total = jm._semantic_pass(job, cancel, force=False)

        assert len(batch_calls) == 1        # one combined attempt first
        assert len(solo_calls) == 2         # then isolated per item
        assert (indexed, skipped, failed, total) == (2, 0, 0, 2)
    finally:
        jm.shutdown(timeout=2.0)
