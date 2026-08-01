"""Pets: species counts, pet-identity groups, and the non-human review queue."""

from __future__ import annotations

from ...services import pets
from ._request import NOT_FOUND, Request


def summary(req: Request) -> dict:
    from ...pets.extract import scan_source as pet_scan_source

    rid = req.root_id
    return pets.pet_summary(req.db(rid), rid, pet_scan_source(req.cfg), req.cfg.detect_video_frames)


def groups(req: Request) -> dict:
    rid = req.root_id
    return pets.pet_groups(req.db(rid), rid, limit=req.limit(120, 500), offset=req.offset())


def detections(req: Request) -> dict:
    rid = req.root_id
    return pets.animal_gallery(
        req.db(rid),
        rid,
        limit=req.limit(120, 500),
        offset=req.offset(),
        unassigned=bool(req.one("unassigned", int, 0)),
    )


def nonhuman(req: Request) -> dict:
    rid = req.root_id
    return pets.nonhuman_review(req.db(rid), rid, limit=req.limit(120, 500), offset=req.offset())


def group(req: Request):
    rid = req.root_id
    g = pets.pet_group(
        req.db(rid),
        int(req.path.rsplit("/", 1)[1]),
        rid,
        limit=req.limit(120, 500),
        offset=req.offset(),
    )
    return g if g else NOT_FOUND
