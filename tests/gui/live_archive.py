"""The live GUI server the route tests run against, and the archive behind it.

Extracted from ``test_api_routes.py`` when that file outgrew the size ratchet.
It holds no assertions: just the HTTP helpers, the seeded archive, and the
``live_server`` fixture -- which ``conftest.py`` re-exports so every module in
this directory can request it by name.

The archive is one named, singleton instance of every entity kind the routes
read or mutate. Everything is a *singleton pair at most* -- one file per
person/pet/place, two of each kind rather than a crowd -- because every
mutation route (merge, rename, reassign, ...) needs only two distinct rows to
prove it is wired, and the whole database is rebuilt fresh per test, so nothing
here doubles as load-bearing shared fixture data.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import NamedTuple
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import factories
import pytest
from helpers import serve_in_thread

from trove.config import Config
from trove.db import database as db
from trove.services import archives, meaning, semantic

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _get(base_url: str, path: str) -> tuple[int, str, bytes]:
    """GET ``path``, returning (status, content-type, body) even on a non-2xx
    response -- ``urlopen`` raises for those, and every assertion in this file
    wants the body to explain a failure rather than a bare traceback."""
    try:
        with urlopen(f"{base_url}{path}", timeout=5) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
    except HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


def _get_ranged(base_url: str, path: str, byte_range: str) -> tuple[int, dict[str, str], bytes]:
    """GET with a ``Range`` header, returning (status, headers, body).

    Separate from ``_get`` rather than an extra argument on it: range tests are
    the only ones that need the response *headers* back, and widening ``_get``
    to return them would change the shape every other call site unpacks.
    """
    req = Request(f"{base_url}{path}", headers={"Range": byte_range})
    try:
        with urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def _post(
    base_url: str, path: str, payload: dict, headers: dict | None = None
) -> tuple[int, bytes]:
    data = json.dumps(payload).encode()
    req = Request(
        f"{base_url}{path}",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except HTTPError as exc:
        return exc.code, exc.read()


# ---------------------------------------------------------------------------
# Fixture archive: one named instance of every entity kind the routes under
# test read or mutate.
# ---------------------------------------------------------------------------


def _write_jpeg(path: Path, color: tuple[int, int, int] = (120, 140, 160)) -> None:
    """A real, small, decodable JPEG on disk. Pillow is a hard dependency of
    this project (thumbnails/, icons.py), so thumbnail/original-serving routes
    can exercise their actual decode path here instead of only the
    can't-decode-so-serve-the-original fallback."""
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 32), color).save(path, "JPEG")


def _named_person(conn, name: str, file_id: int) -> tuple[int, int]:
    """A named person with exactly one face, on ``file_id``. Built by hand
    (not ``factories.add_person(faces=...)``) so the face id is captured --
    /api/faces/reassign and /api/faces/detach need it directly."""
    person_id = factories.add_person(conn, name=name, faces=0)
    face_id = factories.add_face(conn, file_id, person_id=person_id)
    conn.execute(
        "UPDATE persons SET face_count=1, cover_face_id=? WHERE id=?", (face_id, person_id)
    )
    return person_id, face_id


def _named_pet(conn, name: str, file_id: int, species: str = "dog") -> tuple[int, int]:
    """A named pet with exactly one detection. Mirrors ``_named_person``."""
    pet_id = factories.add_pet(conn, name=name, species=species, detections=0)
    detection_id = factories.add_animal_detection(conn, file_id, pet_id=pet_id, species=species)
    conn.execute(
        "UPDATE pets SET detection_count=1, cover_detection_id=? WHERE id=?",
        (detection_id, pet_id),
    )
    return pet_id, detection_id


def _seed_media(conn, root_id: int, ids: dict, _file) -> None:
    """A bare file, and a dated+geotagged file with a byte-identical duplicate.

    The bare one is the target for the item-level edit routes (date, place,
    manual person/pet tagging), which should all work on media nothing has been
    resolved for yet. The dated pair drives summary / timeline / dates-sources /
    map / browse-filters / folders and gives dup_groups a real row to describe.

    The sha256 is a fixed fake value, not a real hash of the (independently
    encoded) JPEG bytes -- dup_summary's "identical" bucket only compares the DB
    column, and this is a routing test, not a hashing one.
    """
    _file("plain", "plain.jpg")

    same_sha = "same-sha-0" * 4
    dated = _file("dated", "2024/dated.jpg", sha256=same_sha)
    factories.add_date(conn, dated, best_datetime="2024-06-01T12:00:00")
    factories.add_geo(conn, dated)
    duplicate = _file("duplicate", "2024/dated_copy.jpg", sha256=same_sha)

    cur = conn.execute(
        """INSERT INTO dup_groups(method, canonical_file_id, member_count,
                                   size_each, redundant_bytes, created_at)
           VALUES('exact', ?, 2, 4, 4, ?)""",
        (dated, factories.FIXED_TIME),
    )
    ids["dup_group"] = cur.lastrowid
    conn.execute(
        "INSERT INTO dup_members(group_id, file_id, role) VALUES(?, ?, 'canonical')",
        (ids["dup_group"], dated),
    )
    conn.execute(
        "INSERT INTO dup_members(group_id, file_id, role) VALUES(?, ?, 'duplicate')",
        (ids["dup_group"], duplicate),
    )


def _seed_people(conn, ids: dict, _file) -> None:
    """Two named singleton clusters, each on their own photo, so a
    merge/rename/reassign/detach test can target either without disturbing the
    other's listing."""
    ids["person_a_photo"] = _file("person_a_photo", "people/ana.jpg")
    ids["person_a"], ids["face_a"] = _named_person(conn, "Ana", ids["person_a_photo"])
    ids["person_b_photo"] = _file("person_b_photo", "people/beto.jpg")
    ids["person_b"], ids["face_b"] = _named_person(conn, "Beto", ids["person_b_photo"])


def _seed_pets(conn, ids: dict, _file) -> None:
    """Mirrors ``_seed_people``."""
    ids["pet_a_photo"] = _file("pet_a_photo", "pets/rocco.jpg")
    ids["pet_a"], ids["detection_a"] = _named_pet(conn, "Rocco", ids["pet_a_photo"])
    ids["pet_b_photo"] = _file("pet_b_photo", "pets/fido.jpg")
    ids["pet_b"], ids["detection_b"] = _named_pet(conn, "Fido", ids["pet_b_photo"])


def _seed_places(conn, root_id: int, ids: dict, _file) -> None:
    """Two NAMED clusters (a name exempts a cluster from config.place_min_media,
    see places._PLACE_EXEMPT), each with one geotagged member so
    place_merge_preview has real coordinates to compare."""
    place_a_photo = _file("place_a_photo", "places/home.jpg")
    factories.add_geo(conn, place_a_photo, lat=-41.13, lon=-71.31)
    ids["place_a"] = factories.add_place(
        conn, "Home", root_id=root_id, lat=-41.13, lon=-71.31, file_ids=[place_a_photo]
    )
    place_b_photo = _file("place_b_photo", "places/cabin.jpg")
    factories.add_geo(conn, place_b_photo, lat=-41.20, lon=-71.40)
    ids["place_b"] = factories.add_place(
        conn, "Cabin", root_id=root_id, lat=-41.20, lon=-71.40, file_ids=[place_b_photo]
    )


def _seed_nonhuman(conn, ids: dict) -> None:
    """A toy/animal the face detector mistook for a person: the review queue
    /api/nonhuman and /api/nonhuman/review act on.

    No factories helper exists for this table, so it is inserted directly
    against the schema.
    """
    cur = conn.execute(
        """INSERT INTO nonhuman_detections(file_id, box_x, box_y, box_w, box_h,
                                            kind, confidence, source, review_status,
                                            created_at)
           VALUES(?, 10, 10, 40, 40, 'toy', 0.7, 'test', 'pending', ?)""",
        (ids["plain"], factories.FIXED_TIME),
    )
    ids["nonhuman"] = cur.lastrowid


def _mark_current_embedder(conn) -> None:
    """Stop archive-open from wiping everything seeded above.

    jobs.open_archive() -> _open_db() unconditionally runs
    faces/migrate_adaface.run_if_needed() on every archive it opens: if the
    archive's app_state doesn't already record the CURRENT embedder version, it
    treats every named person/face here as stale identity data left by a retired
    embedder, snapshots it, and wipes `faces`/`persons` wholesale (see
    snapshot_and_wipe's "wipe" section) -- discovered the hard way when
    live_server's own /api/archive/open setup call silently deleted every face
    the seeding had just inserted. Marking the current version is what a real
    archive already has after its first real detect run, and is the only thing
    that makes open_archive() a no-op migration-wise.
    """
    from trove.faces import backend as face_backend
    from trove.faces import migrate_adaface

    migrate_adaface.mark_embedder(conn, face_backend.EMBEDDER_VERSION)


def seed_archive(conn, root_id: int, source_dir: Path) -> dict:
    """Populate one archive database with everything the routes under test need.

    Returns a dict of the ids created, keyed by role ("plain", "person_a",
    "place_b", ...), which the tests index into rather than hard-coding numbers.
    """
    ids: dict[str, int | str] = {}

    def _file(key: str, rel_path: str, **kw) -> int:
        fid = factories.add_file(conn, root_id=root_id, rel_path=rel_path, **kw)
        _write_jpeg(source_dir / rel_path)
        ids[key] = fid
        return fid

    _seed_media(conn, root_id, ids, _file)
    _seed_people(conn, ids, _file)
    _seed_pets(conn, ids, _file)
    _seed_places(conn, root_id, ids, _file)
    _seed_nonhuman(conn, ids)
    _mark_current_embedder(conn)
    return ids


class LiveServer(NamedTuple):
    base_url: str
    ids: dict
    cfg: Config


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    """A live GUI server over the archive ``seed_archive`` builds.

    Function-scoped -- rebuilt fresh for every test -- on purpose: a wider
    scope would let a *later* test's own XDG_DATA_HOME (set per-test by the
    autouse ``isolate_app_data`` fixture in tests/conftest.py) pull this
    server's already-open database out from under it, since
    ``Config.archive_db_path`` re-resolves the environment on every call
    rather than caching it at construction. Within one test the env is
    stable for the test's whole duration, so function scope sidesteps the
    whole problem instead of fighting it. The rebuild cost is a handful of
    sqlite inserts and a thread start -- see the durations note in the report
    for what that actually costs.
    """
    # Seeding writes real images, so the whole tier needs Pillow. Guarded in
    # the fixture rather than at module scope because this module is imported
    # by conftest.py, where a collection-time skip is an error rather than a
    # skip -- and without the guard an install without the media extra reports
    # ninety-odd errors instead of ninety-odd skips.
    pytest.importorskip("PIL.Image")
    cfg = Config.load()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    registered = archives.add_archive(cfg, str(source_dir))
    assert "id" in registered, registered
    root_id = registered["id"]

    conn = db.connect(cfg.archive_db_path(root_id))
    try:
        ids = seed_archive(conn, root_id, source_dir)
        conn.commit()
    finally:
        conn.close()
    ids["root_id"] = root_id

    # A second, unregistered real directory for the one POST route that
    # registers a *new* archive (/api/archives) without disturbing the one
    # already set up above.
    extra_dir = tmp_path / "another_source"
    extra_dir.mkdir()
    ids["extra_archive_path"] = str(extra_dir)

    # The real /api/browse/semantic/search path calls this to turn the typed
    # query into a vector. Unstubbed, it loads the actual SigLIP text tower
    # (trove/embeddings/backend.py:load_text), which downloads
    # ~372 MB the first time -- and this test's XDG-isolated cache dir never
    # has it cached, so that would be a real network fetch. CONTRIBUTING's
    # no-network rule (and this sandboxed test run) both forbid that, so only
    # the embedding step is stubbed to a fixed unit vector; the rest of the
    # route (query parsing, search.semantic_search against whatever's in
    # semantic_embeddings) still runs for real.
    monkeypatch.setattr(
        semantic, "embed_queries", lambda cfg, qs: [[1.0] + [0.0] * 767 for _ in qs]
    )
    # And the same for the other search, for the same reason:
    # /api/browse/text/search embeds the typed query through the 118 MB text
    # encoder, which this XDG-isolated cache has never seen. The BM25 half of
    # that route -- which is the half with no model behind it -- still runs
    # for real against whatever is in the index.
    monkeypatch.setattr(meaning, "embed_queries", lambda cfg, qs: [[1.0] + [0.0] * 383 for _ in qs])

    with serve_in_thread(cfg) as httpd:
        host, port = httpd.server_address
        base_url = f"http://{host}:{port}"
        # /api/item/*, most /api/faces/* and /api/pet* mutations, and every
        # media-serving prefix resolve their archive via
        # jobs.current_root_id() rather than a ?root= param (see server.py's
        # "per-archive resolution" comment) -- so the archive has to be open,
        # not just registered, before any of those routes can answer.
        status, body = _post(base_url, "/api/archive/open", {"root_id": root_id})
        assert status == 200, body
        yield LiveServer(base_url, ids, cfg)
