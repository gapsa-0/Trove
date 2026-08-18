"""Face/person browsing: the summary, the person list, merge suggestions, and
one person's detail page."""

from __future__ import annotations

from typing import Any

from ...db import database as db
from ...services import edit_log, merging, people, people_edit, people_merge
from ._request import NOT_FOUND, Json, Request, ok_or_error


def summary(req: Request) -> dict:
    """Face/person totals for the Faces overview."""
    rid = req.root_id
    return people.face_summary(req.db(rid), rid, req.cfg.detect_video_frames)


def persons(req: Request) -> dict:
    """Paginated list of person clusters. ``?hidden=1`` lists the hidden ones."""
    rid = req.root_id
    return people.face_persons(
        req.db(rid),
        rid,
        limit=req.limit(120, 500),
        offset=req.offset(),
        hidden=req.one("hidden", int, 0) == 1,
    )


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


def merge_targets(req: Request) -> dict | Json:
    """The named clusters a "Merge with…" picker can offer.

    One route for people, pets and places because it is one question asked of
    three tables, and services/merging.py is where that symmetry already lives.
    """
    rid = req.root_id
    return {
        "targets": merging.named_targets(
            req.db(rid),
            req.one("entity", str, "person"),
            req.one("exclude", int, 0) or None,
        )
    }


def history(req: Request) -> dict:
    """Recent edits to one person or pet, for the history popover.

    ``name`` is passed through because an id alone stops resolving after a
    recluster; see services/edit_log.py.
    """
    rid = req.root_id
    return {
        "entries": edit_log.entries_for(
            req.db(rid),
            req.one("entity", str, edit_log.PERSON),
            req.one("id", int, 0),
            req.one("name", str, ""),
            limit=req.limit(20, 100),
        )
    }


def undo_edit(req: Request) -> Json:
    """Reverse one history entry, whatever kind it is."""
    res = db.write_with_retry(
        lambda: edit_log.undo(req.db(req.open_root_id), req.body.get("entry_id"))
    )
    # An undone merge asks for a recluster the same way unmerge does below.
    if res.get("recluster") and req.jobs.current_root_id():
        req.jobs.start("face_cluster", req.jobs.current_root_id())
    return ok_or_error(res)


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


def name_face(req: Request) -> Json:
    """Name the person a face in the open photo belongs to, from the photo."""
    res = db.write_with_retry(
        lambda: people_edit.name_face(
            req.db(req.open_root_id),
            req.body.get("face_id"),
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
        lambda: people_merge.merge_persons(
            req.db(req.open_root_id), req.body.get("a"), req.body.get("b"), req.body.get("name")
        )
    )
    return ok_or_error(res)


def unmerge(req: Request) -> Json:
    """Undo a person merge and, if needed, kick off a recluster."""
    res = db.write_with_retry(
        lambda: people_merge.unmerge_persons(req.db(req.open_root_id), req.body.get("merge_id"))
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
        lambda: people_merge.set_persons_different(
            req.db(req.open_root_id), req.body.get("a"), req.body.get("b")
        )
    )
    return ok_or_error(res)


def skip(req: Request) -> Json:
    """Record that a 'same person?' pair was reviewed and left undecided, so it
    drops out of the suggestions queue."""
    res = db.write_with_retry(
        lambda: people_merge.set_persons_skip(
            req.db(req.open_root_id), req.body.get("a"), req.body.get("b")
        )
    )
    return ok_or_error(res)


def hide(req: Request) -> Json:
    """Take a cluster off the People screen.

    ``reason=not_person`` marks the detections as a doll/animal/cartoon and
    drops the cluster; ``reason=unknown`` hides a real person, leaving their
    faces in clustering. See people_edit.hide_person for why those are not the
    same operation.
    """
    res = db.write_with_retry(
        lambda: people_edit.hide_person(
            req.db(req.open_root_id),
            req.body.get("person_id"),
            req.body.get("kind", "false_detection"),
            req.body.get("reason", "not_person"),
        )
    )
    return ok_or_error(res)


def set_cover(req: Request) -> Json:
    """Choose which of a person's photos represents them."""
    res = db.write_with_retry(
        lambda: people_edit.set_person_cover(
            req.db(req.open_root_id), req.body.get("person_id"), req.body.get("face_id")
        )
    )
    return ok_or_error(res)


def unhide(req: Request) -> Json:
    """Put a hidden cluster back on the People screen."""
    res = db.write_with_retry(
        lambda: people_edit.unhide_person(req.db(req.open_root_id), req.body.get("person_id"))
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
