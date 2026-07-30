"""Pending-work counts: how much detection work is left to do.

The pipeline scheduler polls these (DB-only, no disk walk) to decide which
stage -- faces, pets, or the fused detect pass -- is startable next.
"""

from __future__ import annotations

from ._common import _NOT_HIDDEN, _root_clause, reading


@reading
def faces_pending(conn, root_id=None) -> int:
    """Present images not yet face-scanned (DB-only, no disk walk), used by the
    auto-scheduler to decide whether to queue a faces job."""
    rc, rp = _root_clause(root_id)
    # _NOT_HIDDEN, matching faces.extract.pending_count: the face pass only
    # touches canonical images, so counting unscanned duplicates here would
    # make the scheduler queue faces jobs that find nothing to do, forever.
    return conn.execute(
        f"""SELECT COUNT(*) FROM files f
            LEFT JOIN face_scan s ON s.file_id=f.id
            WHERE s.file_id IS NULL AND {_NOT_HIDDEN} AND f.media_type='image'{rc}""",
        rp,
    ).fetchone()[0]


@reading
def pets_pending(conn, root_id=None, model_source=None) -> int:
    rc, rp = _root_clause(root_id)
    model_clause = ""
    params = []
    if model_source is not None:
        model_clause = " OR s.model_source IS NOT ?"
        params.append(model_source)
    params.extend(rp)
    return conn.execute(
        f"""SELECT COUNT(*) FROM files f
            LEFT JOIN pet_scan s ON s.file_id=f.id
            WHERE (s.file_id IS NULL OR s.source_sha256 IS NOT f.sha256
                   {model_clause})
              AND {_NOT_HIDDEN} AND f.media_type='image'{rc}""",
        params,
    ).fetchone()[0]


@reading
def detect_pending(conn, root_id=None, model_source=None, detect_video_frames: int = 0) -> int:
    """Present canonical media missing a current face OR pet scan — the fused
    detect stage's backlog (mirrors detect.extract.pending_count). One file is
    'pending' if either detector still owes it work, so the scheduler keeps the
    single detect stage running until both are drained.

    ``detect_video_frames`` mirrors ``cfg.detect_video_frames``: videos only
    count toward the backlog when it is > 0, otherwise the stage would never
    reach "up to date" while video detection is disabled."""
    rc, rp = _root_clause(root_id)
    media_types = "('image','video')" if detect_video_frames > 0 else "('image')"
    params = [model_source, *rp]
    return conn.execute(
        f"""SELECT COUNT(*) FROM files f
            LEFT JOIN face_scan fs ON fs.file_id=f.id
            LEFT JOIN pet_scan ps ON ps.file_id=f.id
            WHERE (fs.file_id IS NULL
                   OR ps.file_id IS NULL
                   OR ps.source_sha256 IS NOT f.sha256
                   OR ps.model_source IS NOT ?)
              AND {_NOT_HIDDEN} AND f.media_type IN {media_types}{rc}""",
        params,
    ).fetchone()[0]
