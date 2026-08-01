"""Face/person browsing: the summary, the person list, merge suggestions, and
one person's detail page."""

from __future__ import annotations

from ...services import people
from ._request import NOT_FOUND, Request


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
