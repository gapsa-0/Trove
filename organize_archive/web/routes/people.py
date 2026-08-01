"""Face/person browsing: the summary, the person list, merge suggestions, and
one person's detail page."""

from __future__ import annotations

from ...db import database as db
from ...services import people
from ._request import NOT_FOUND, Json, Request, ok_or_error


def summary(req: Request) -> dict:
    rid = req.root_id
    return people.face_summary(req.db(rid), rid, req.cfg.detect_video_frames)


def persons(req: Request) -> dict:
    rid = req.root_id
    return people.face_persons(req.db(rid), rid, limit=req.limit(120, 500), offset=req.offset())


def suggestions(req: Request) -> dict:
    rid = req.root_id
    return people.person_suggestions(req.db(rid), rid, limit=req.limit(40, 200))


def person(req: Request):
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
    res = db.write_with_retry(
        lambda: people.rename_person(
            req.db(req.open_root_id),
            req.body.get("person_id"),
            (req.body.get("name") or "").strip(),
        )
    )
    return ok_or_error(res)


def reassign(req: Request) -> Json:
    res = db.write_with_retry(
        lambda: people.reassign_face(
            req.db(req.open_root_id), req.body.get("face_id"), req.body.get("person_id")
        )
    )
    return ok_or_error(res)


def merge(req: Request) -> Json:
    res = db.write_with_retry(
        lambda: people.merge_persons(
            req.db(req.open_root_id), req.body.get("a"), req.body.get("b"), req.body.get("name")
        )
    )
    return ok_or_error(res)


def unmerge(req: Request) -> Json:
    res = db.write_with_retry(
        lambda: people.unmerge_persons(req.db(req.open_root_id), req.body.get("merge_id"))
    )
    if res.get("recluster") and req.jobs.current_root_id():
        req.jobs.start("face_cluster", req.jobs.current_root_id())
    return ok_or_error(res)


def detach(req: Request) -> Json:
    res = db.write_with_retry(
        lambda: people.detach_file_from_person(
            req.db(req.open_root_id), req.body.get("person_id"), req.body.get("file_id")
        )
    )
    return ok_or_error(res)


def mark_different(req: Request) -> Json:
    res = db.write_with_retry(
        lambda: people.set_persons_different(
            req.db(req.open_root_id), req.body.get("a"), req.body.get("b")
        )
    )
    return ok_or_error(res)


def skip(req: Request) -> Json:
    res = db.write_with_retry(
        lambda: people.set_persons_skip(
            req.db(req.open_root_id), req.body.get("a"), req.body.get("b")
        )
    )
    return ok_or_error(res)


def hide(req: Request) -> Json:
    res = db.write_with_retry(
        lambda: people.hide_person(
            req.db(req.open_root_id),
            req.body.get("person_id"),
            req.body.get("kind", "false_detection"),
        )
    )
    return ok_or_error(res)


def add_person(req: Request) -> Json:
    res = db.write_with_retry(
        lambda: people.add_person_to_file(
            req.db(req.open_root_id), req.body.get("person_id"), req.body.get("file_id")
        )
    )
    return ok_or_error(res)


def remove_person(req: Request) -> Json:
    res = db.write_with_retry(
        lambda: people.remove_person_from_file(
            req.db(req.open_root_id), req.body.get("person_id"), req.body.get("file_id")
        )
    )
    return ok_or_error(res)
