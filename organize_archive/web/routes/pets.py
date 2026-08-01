"""Pets: species counts, pet-identity groups, and the non-human review queue."""

from __future__ import annotations

from ...db import database as db
from ...services import pets
from ._request import NOT_FOUND, Json, Request, ok_or_error


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


def rename_pet(req: Request) -> Json:
    res = db.write_with_retry(
        lambda: pets.rename_pet(
            req.db(req.open_root_id),
            req.body.get("pet_id"),
            (req.body.get("name") or "").strip(),
        )
    )
    return ok_or_error(res)


def merge(req: Request) -> Json:
    res = db.write_with_retry(
        lambda: pets.merge_pets(
            req.db(req.open_root_id), req.body.get("a"), req.body.get("b"), req.body.get("name")
        )
    )
    return ok_or_error(res)


def unmerge(req: Request) -> Json:
    res = db.write_with_retry(
        lambda: pets.unmerge_pets(req.db(req.open_root_id), req.body.get("merge_id"))
    )
    if res.get("recluster") and req.jobs.current_root_id():
        req.jobs.start("pet_cluster", req.jobs.current_root_id())
    return ok_or_error(res)


def review_nonhuman(req: Request) -> Json:
    res = db.write_with_retry(
        lambda: pets.review_nonhuman(
            req.db(req.open_root_id),
            req.body.get("detection_id"),
            req.body.get("verdict", "confirmed"),
        )
    )
    if res.get("status") == "human" and res.get("root_id"):
        req.jobs.start("face_cluster", res["root_id"])
    return ok_or_error(res)


def add_pet(req: Request) -> Json:
    res = db.write_with_retry(
        lambda: pets.add_pet_to_file(
            req.db(req.open_root_id), req.body.get("pet_id"), req.body.get("file_id")
        )
    )
    return ok_or_error(res)


def remove_pet(req: Request) -> Json:
    res = db.write_with_retry(
        lambda: pets.remove_pet_from_file(
            req.db(req.open_root_id), req.body.get("pet_id"), req.body.get("file_id")
        )
    )
    return ok_or_error(res)
