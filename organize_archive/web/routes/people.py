"""Face/person browsing: the summary, the person list, merge suggestions, and
one person's detail page."""

from __future__ import annotations

from typing import Any

from ...db import database as db
from ...services import people, people_edit
from ._request import NOT_FOUND, Json, Request, ok_or_error


def summary(req: Request) -> dict:
    """Face/person totals for the Faces overview."""
    rid = req.root_id
    return people.face_summary(req.db(rid), rid, req.cfg.detect_video_frames)


def persons(req: Request) -> dict:
    """Paginated list of person clusters."""
    rid = req.root_id
    return people.face_persons(req.db(rid), rid, limit=req.limit(120, 500), offset=req.offset())


def suggestions(req: Request) -> dict:
    """The 'same person?' review queue: candidate cluster pairs the automatic pass left apart."""
    rid = req.root_id
    return people.person_suggestions(req.db(rid), rid, limit=req.limit(40, 200))


def person(req: Request) -> dict[str, Any] | Json:
    """One person's detail page: their faces, paginated."""
    rid = req.root_id
    p = people.face_person(
        req.db(rid),
        int(req.path.rsplit("/", 1)[1]),
        rid,
        limit=req.limit(120, 500),
        offset=req.offset(),
    )
    return p if p else NOT_FOUND


def rename_person(req: Request) -> Json:
    """Rename a person cluster."""
    res = db.write_with_retry(
        lambda: people_edit.rename_person(
            req.db(req.open_root_id),
            req.body.get("person_id"),
            (req.body.get("name") or "").strip(),
        )
    )
    return ok_or_error(res)


def reassign(req: Request) -> Json:
    """Move one face onto a named person and pin it there so re-clustering keeps it."""
    res = db.write_with_retry(
        lambda: people_edit.reassign_face(
            req.db(req.open_root_id), req.body.get("face_id"), req.body.get("person_id")
        )
    )
    return ok_or_error(res)


def merge(req: Request) -> Json:
    """Merge two person clusters the user confirmed are the same, immediately and durably."""
    res = db.write_with_retry(
        lambda: people_edit.merge_persons(
            req.db(req.open_root_id), req.body.get("a"), req.body.get("b"), req.body.get("name")
        )
    )
    return ok_or_error(res)


def unmerge(req: Request) -> Json:
    """Undo a person merge and, if needed, kick off a recluster."""
    res = db.write_with_retry(
        lambda: people_edit.unmerge_persons(req.db(req.open_root_id), req.body.get("merge_id"))
    )
    if res.get("recluster") and req.jobs.current_root_id():
        req.jobs.start("face_cluster", req.jobs.current_root_id())
    return ok_or_error(res)


def detach(req: Request) -> Json:
    """Release every face of one file from a person and durably block them from
    drifting back."""
    res = db.write_with_retry(
        lambda: people_edit.detach_file_from_person(
            req.db(req.open_root_id), req.body.get("person_id"), req.body.get("file_id")
        )
    )
    return ok_or_error(res)


def mark_different(req: Request) -> Json:
    """Record that two person clusters are confirmed NOT the same, so auto-merge
    never proposes them again."""
    res = db.write_with_retry(
        lambda: people_edit.set_persons_different(
            req.db(req.open_root_id), req.body.get("a"), req.body.get("b")
        )
    )
    return ok_or_error(res)


def skip(req: Request) -> Json:
    """Record that a 'same person?' pair was reviewed and left undecided, so it
    drops out of the suggestions queue."""
    res = db.write_with_retry(
        lambda: people_edit.set_persons_skip(
            req.db(req.open_root_id), req.body.get("a"), req.body.get("b")
        )
    )
    return ok_or_error(res)


def hide(req: Request) -> Json:
    """Mark a cluster as not a person (animal/toy/cartoon/false detection) and drop it."""
    res = db.write_with_retry(
        lambda: people_edit.hide_person(
            req.db(req.open_root_id),
            req.body.get("person_id"),
            req.body.get("kind", "false_detection"),
        )
    )
    return ok_or_error(res)


def add_person(req: Request) -> Json:
    """Tag a file with a named person by hand, for media where no face was detected."""
    res = db.write_with_retry(
        lambda: people_edit.add_person_to_file(
            req.db(req.open_root_id), req.body.get("person_id"), req.body.get("file_id")
        )
    )
    return ok_or_error(res)


def remove_person(req: Request) -> Json:
    """Remove a hand-added person tag from a file."""
    res = db.write_with_retry(
        lambda: people_edit.remove_person_from_file(
            req.db(req.open_root_id), req.body.get("person_id"), req.body.get("file_id")
        )
    )
    return ok_or_error(res)
