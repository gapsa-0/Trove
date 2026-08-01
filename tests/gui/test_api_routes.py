"""Route-existence coverage for organize_archive/web/server.py.

``server.py`` sits at 16% statement coverage: ``do_GET``/``do_POST`` are two
long if-elif chains covering 66 routes (39 GET, 27 POST -- see
``plan/repo-overhaul/baseline/api-routes.txt``), and almost none of the
branches in that chain ever execute in the suite. The next refactor stage
(08) rewrites both chains into a route table; without a test per route, a
route silently dropped or mistyped during that rewrite would not fail a
single test.

The trap this file is built around: an unknown path and a legitimate
"no such record" answer return the *same* body (``{"error": "not found"}``,
404 -- see server.py:453/735 for the fall-throughs and e.g. server.py:425 for
a genuine miss). So "not 404" proves nothing. Every test here instead spins
up a real archive with real rows (``live_server`` below) and asserts a GET
returns 200 with the JSON shape that route actually produces, or a POST
returns its real success body -- not merely "didn't fall through".

Static asset and media-serving routes (no JSON shape to check) instead
assert 200 and a sane content-type. The one static case with a genuinely
fixed body (``/api/settings``) gets an exact-equality test.

A drift guard at the bottom parses server.py's own ``path ==`` / ``path in
(...)`` / ``path.startswith(...)`` conditions and checks the result against
the literal route lists this file declares -- so an added-and-forgotten (or
silently dropped) route fails a test here too, not just a future coverage
report.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import NamedTuple
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import factories
import pytest
from helpers import serve_in_thread

from organize_archive.config import Config
from organize_archive.db import database as db
from organize_archive.errors import ModelUnavailableError
from organize_archive.services import archives, semantic
from organize_archive.web import server

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


def _seed_archive(conn, root_id: int, source_dir: Path) -> dict:
    """Populate one archive database with a named, singleton instance of every
    entity kind server.py's routes read or mutate: a bare file, a dated and
    geotagged file with an exact duplicate, two named people, two named pets,
    two named places, and a non-human (toy/animal) detection.

    Everything is a *singleton pair at most* -- one file per person/pet/place,
    two of each kind rather than a crowd -- because every mutation route
    (merge, rename, reassign, ...) needs only two distinct rows to prove it is
    wired, and this whole database is rebuilt fresh per test (see
    ``live_server``), so nothing here needs to double as load-bearing shared
    fixture data the way it might in a slower, wider-scoped suite.
    """
    ids: dict[str, int | str] = {}

    def _file(key: str, rel_path: str, **kw) -> int:
        fid = factories.add_file(conn, root_id=root_id, rel_path=rel_path, **kw)
        _write_jpeg(source_dir / rel_path)
        ids[key] = fid
        return fid

    # Bare file: no date, no geo -- the target for the item-level edit routes
    # (date, place, manual person/pet tagging), which should all work on media
    # nothing has been resolved for yet.
    _file("plain", "plain.jpg")

    # Dated + geotagged, plus a byte-identical duplicate: drives
    # summary/timeline/dates-sources/map/browse-filters/folders and gives
    # dup_groups/dup_members a real row to describe. The sha256 is a fixed
    # fake value, not a real hash of the (independently-encoded) JPEG bytes --
    # dup_summary's "identical" bucket only compares the DB column, and this
    # is a routing test, not a hashing one.
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

    # People: two named singleton clusters, each on their own photo, so a
    # merge/rename/reassign/detach test can target either without disturbing
    # the other's listing.
    ids["person_a_photo"] = _file("person_a_photo", "people/ana.jpg")
    ids["person_a"], ids["face_a"] = _named_person(conn, "Ana", ids["person_a_photo"])
    ids["person_b_photo"] = _file("person_b_photo", "people/beto.jpg")
    ids["person_b"], ids["face_b"] = _named_person(conn, "Beto", ids["person_b_photo"])

    # Pets: mirrors people.
    ids["pet_a_photo"] = _file("pet_a_photo", "pets/rocco.jpg")
    ids["pet_a"], ids["detection_a"] = _named_pet(conn, "Rocco", ids["pet_a_photo"])
    ids["pet_b_photo"] = _file("pet_b_photo", "pets/fido.jpg")
    ids["pet_b"], ids["detection_b"] = _named_pet(conn, "Fido", ids["pet_b_photo"])

    # Places: two NAMED clusters (a name exempts a cluster from
    # config.place_min_media, see places._PLACE_EXEMPT), each with one
    # geotagged member so place_merge_preview has real coordinates to compare.
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

    # A non-human detection (toy/animal the face detector mistook for a
    # person): the review queue /api/nonhuman and /api/nonhuman/review act on.
    # No factories helper exists for this table (things_to_fix candidate --
    # see the report); inserted directly against the schema instead.
    cur = conn.execute(
        """INSERT INTO nonhuman_detections(file_id, box_x, box_y, box_w, box_h,
                                            kind, confidence, source, review_status,
                                            created_at)
           VALUES(?, 10, 10, 40, 40, 'toy', 0.7, 'test', 'pending', ?)""",
        (ids["plain"], factories.FIXED_TIME),
    )
    ids["nonhuman"] = cur.lastrowid

    # jobs.open_archive() -> _open_db() unconditionally runs
    # faces/migrate_adaface.run_if_needed() on every archive it opens: if the
    # archive's app_state doesn't already record the CURRENT embedder version,
    # it treats every named person/face here as stale identity data left by a
    # retired embedder, snapshots it, and wipes `faces`/`persons` wholesale
    # (see snapshot_and_wipe's "wipe" section) -- discovered the hard way when
    # live_server's own /api/archive/open setup call silently deleted every
    # face this function had just inserted. Marking the current version here
    # is what a real archive already has after its first real detect run, and
    # is the only thing that makes open_archive() a no-op migration-wise.
    from organize_archive.faces import backend as face_backend
    from organize_archive.faces import migrate_adaface

    migrate_adaface.mark_embedder(conn, face_backend.EMBEDDER_VERSION)

    return ids


class LiveServer(NamedTuple):
    base_url: str
    ids: dict
    cfg: Config


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    """A live GUI server over the archive ``_seed_archive`` builds.

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
    cfg = Config.load()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    registered = archives.add_archive(cfg, str(source_dir))
    assert "id" in registered, registered
    root_id = registered["id"]

    conn = db.connect(cfg.archive_db_path(root_id))
    try:
        ids = _seed_archive(conn, root_id, source_dir)
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
    # (organize_archive/embeddings/backend.py:load_text), which downloads
    # ~372 MB the first time -- and this test's XDG-isolated cache dir never
    # has it cached, so that would be a real network fetch. CLAUDE.md's
    # local-only rule (and this sandboxed test run) both forbid that, so only
    # the embedding step is stubbed to a fixed unit vector; the rest of the
    # route (query parsing, search.semantic_search against whatever's in
    # semantic_embeddings) still runs for real.
    monkeypatch.setattr(
        semantic, "embed_queries", lambda cfg, qs: [[1.0] + [0.0] * 767 for _ in qs]
    )

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


# ---------------------------------------------------------------------------
# GET -- static assets (6 of the 11 non-/api routes)
# ---------------------------------------------------------------------------

STATIC_GET_CASES = [
    pytest.param("/", "text/html", id="GET / (exact)"),
    pytest.param("/index.html", "text/html", id="GET /index.html (exact)"),
    pytest.param(
        "/manifest.webmanifest", "application/manifest+json", id="GET /manifest.webmanifest (exact)"
    ),
    pytest.param("/sw.js", "text/javascript", id="GET /sw.js (exact)"),
    pytest.param("/icon-192.png", "image/png", id="GET /icon- (prefix)"),
    pytest.param("/vendor/leaflet.css", "text/css", id="GET /vendor/ (prefix)"),
]


@pytest.mark.parametrize("path, content_type_prefix", STATIC_GET_CASES)
def test_every_static_get_route_answers_with_its_content_type(
    live_server, path, content_type_prefix
):
    status, content_type, body = _get(live_server.base_url, path)
    assert status == 200, body
    assert content_type.startswith(content_type_prefix), content_type
    assert body


# ---------------------------------------------------------------------------
# GET -- media-serving prefixes (5 of the 11 non-/api routes)
# ---------------------------------------------------------------------------


def test_get_thumb_serves_a_real_thumbnail(live_server):
    status, content_type, body = _get(live_server.base_url, f"/thumb/{live_server.ids['plain']}")
    assert status == 200, body
    assert content_type.startswith("image/")
    assert body


def test_get_archivethumb_serves_by_explicit_root_and_file_id(live_server):
    ids = live_server.ids
    status, content_type, body = _get(
        live_server.base_url, f"/archivethumb/{ids['root_id']}/{ids['plain']}"
    )
    assert status == 200, body
    assert content_type.startswith("image/")


def test_get_facethumb_serves_a_face_crop(live_server):
    status, content_type, body = _get(
        live_server.base_url, f"/faceThumb/{live_server.ids['face_a']}"
    )
    assert status == 200, body
    assert content_type.startswith("image/")


def test_get_animalthumb_serves_a_detection_crop(live_server):
    status, content_type, body = _get(
        live_server.base_url, f"/animalThumb/{live_server.ids['detection_a']}"
    )
    assert status == 200, body
    assert content_type.startswith("image/")


def test_get_file_serves_the_original_bytes(live_server):
    status, content_type, body = _get(live_server.base_url, f"/file/{live_server.ids['plain']}")
    assert status == 200, body
    assert content_type.startswith("image/")
    assert body


def test_get_root_scoped_route_without_root_param_answers_400(live_server):
    """A root-scoped GET called without ``?root=`` answers 400, like every
    other validation failure in this file.

    This test used to assert the opposite, and was named
    ``..._drops_the_connection_silently``. Every GET route that resolves a
    ``root`` id and then asks for its database (summary, timeline,
    dates/sources, every /api/map/*, faces/summary, pets*, pet/*, nonhuman,
    faces/persons, faces/suggestions, faces/person/*, dups*, media, browse/*)
    raises ``ValueError("root is required")`` when the param is missing --
    and ``do_GET``'s outer ``except (ValueError, BrokenPipeError): pass``
    swallowed it having sent nothing at all, so the client saw
    ``RemoteDisconnected`` instead of a response. POST answered 400 for the
    same mistake the whole time; only GET had the hole.

    It is pinned on ``/api/summary``, but the fix is structural: both methods
    now answer through one dispatcher, so no route can reacquire this.
    """
    status, _content_type, body = _get(live_server.base_url, "/api/summary")
    assert status == 400, body
    assert json.loads(body) == {"error": "root is required"}


# ---------------------------------------------------------------------------
# The dispatcher's four outcomes. Everything above asks "does this route work";
# these ask "what happens when one doesn't", which is the half that used to
# have no coverage at all and where the silent-drop bug above lived.
#
# The failing handlers are injected into the live tables with monkeypatch: the
# server runs in a thread in this same process, so it looks up the same dict
# object these tests mutate. Faking the failure is the only way in -- no real
# route can be made to raise on demand without changing shipped code.
# ---------------------------------------------------------------------------


def test_an_unknown_get_path_answers_404(live_server):
    status, _content_type, body = _get(live_server.base_url, "/api/no-such-route")
    assert status == 404, body
    assert json.loads(body) == {"error": "not found"}


def test_an_unknown_post_path_answers_404(live_server):
    status, body = _post(live_server.base_url, "/api/no-such-route", {})
    assert status == 404, body
    assert json.loads(body) == {"error": "not found"}


def test_a_trove_error_answers_400_with_its_message(live_server, monkeypatch):
    """``TroveError`` is the "raised on purpose, message written for a person"
    base class, so its text goes to the client verbatim rather than becoming an
    anonymous 500."""

    def raiser(req):
        raise ModelUnavailableError("the face model is not installed")

    monkeypatch.setitem(server.routes.GET_ROUTES, "/api/health", raiser)
    status, _content_type, body = _get(live_server.base_url, "/api/health")
    assert status == 400, body
    assert json.loads(body) == {"error": "the face model is not installed"}


def test_an_unexpected_error_answers_500_and_logs_a_traceback(live_server, monkeypatch, caplog):
    """The other half of the same split: an error we did not raise on purpose
    is our bug, so it gets a traceback in the log. Before this stage a handler
    blowing up produced whatever ``BaseHTTPRequestHandler`` does by default and
    logged nothing."""

    def raiser(req):
        raise KeyError("cover_face_id")

    monkeypatch.setitem(server.routes.GET_ROUTES, "/api/health", raiser)
    with caplog.at_level(logging.ERROR, logger="organize_archive.web.server"):
        status, _content_type, body = _get(live_server.base_url, "/api/health")

    assert status == 500, body
    assert "cover_face_id" in json.loads(body)["error"]
    assert any("Traceback" in rec.exc_text for rec in caplog.records if rec.exc_text)


# ---------------------------------------------------------------------------
# Hardening. This server reads a user's entire photo archive and listens on a
# predictable loopback port, so the two questions worth pinning are "can
# another site drive it" and "can a request name a file outside the archive".
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "origin",
    [
        pytest.param("http://evil.example", id="another site"),
        pytest.param("null", id="sandboxed iframe / file://"),
        # Same host, different port: a *different* local app, which is exactly
        # the neighbour a loopback-only check would wrongly trust.
        pytest.param("http://127.0.0.1:1", id="another local server"),
    ],
)
def test_a_cross_origin_post_is_refused(live_server, origin):
    """Loopback is not a security boundary: a tab on any website can POST to
    127.0.0.1 and the browser will send it.

    A JSON body would normally force a CORS preflight this server never
    answers -- but ``fetch`` with ``text/plain`` is a simple request that skips
    the preflight entirely, and ``_read_json_body`` parses the body whatever
    the content type claims. So the ``Origin`` check is what actually stops it.
    """
    status, body = _post(live_server.base_url, "/api/archive/close", {}, headers={"Origin": origin})
    assert status == 403, body
    assert json.loads(body) == {"error": "cross-origin request refused"}


def test_a_same_origin_post_is_allowed(live_server):
    """The other half, and the one that matters for not breaking the app: the
    GUI's own fetches carry an ``Origin`` matching ``Host``, and so does the
    Electron shell, which loads the app over http from this very server.

    ``base_url`` is exactly that origin -- scheme, host and port, no path --
    which is what the browser would send."""
    status, body = _post(
        live_server.base_url,
        "/api/archive/close",
        {},
        headers={"Origin": live_server.base_url},
    )
    assert status == 200, body
    assert json.loads(body) == {"ok": True}


def test_a_post_with_no_origin_header_is_allowed(live_server):
    """No ``Origin`` means the caller is not a browser -- curl, a script, this
    suite. Those are not the confused deputy the check defends against, and
    refusing them would break every non-browser client for no gain."""
    status, body = _post(live_server.base_url, "/api/archive/close", {})
    assert status == 200, body


def test_a_cross_origin_get_is_still_allowed(live_server):
    """GET is deliberately unchecked. It changes nothing, and a cross-origin
    caller cannot read the reply without the CORS headers this server never
    sends -- so refusing it would buy nothing and could break the shell."""
    req = Request(f"{live_server.base_url}/api/health", headers={"Origin": "http://evil.example"})
    with urlopen(req, timeout=5) as resp:
        assert resp.status == 200


# Deep enough to reach the filesystem root from web/vendor/ whatever the repo
# is checked out under. This is load-bearing: an earlier draft of these tests
# used three "..", which cannot climb out of the repo, so they passed against a
# deliberately broken server too and proved nothing. Any payload here must be
# able to actually reach /etc/passwd if the route lets it.
_UP = "../" * 12

TRAVERSAL_PAYLOADS = [
    pytest.param(f"/vendor/{_UP}etc/passwd", id="dot-dot segments"),
    pytest.param(f"/vendor/{_UP.replace('/', '%2f')}etc%2fpasswd", id="percent-encoded slashes"),
    pytest.param("/vendor/" + "....//" * 12 + "etc/passwd", id="doubled dots"),
    pytest.param("/vendor/..", id="bare dot-dot"),
    pytest.param("/vendor/", id="empty name"),
    pytest.param(f"/vendor/{_UP}etc/passwd%00.css", id="null byte"),
]


@pytest.mark.parametrize("path", TRAVERSAL_PAYLOADS)
def test_vendor_route_cannot_escape_its_directory(live_server, path):
    """``/vendor/<name>`` is the only route that builds a filesystem path out of
    the request rather than out of a database id, so it is this server's only
    path-traversal surface.

    Two things stop it, and they are checked together because either alone
    would be enough today and neither is obviously permanent: the handler takes
    only the last ``/``-separated segment, and it refuses a segment containing
    ``..``.

    Verified non-vacuous by mutation, and the result is worth recording.
    Breaking *only* the segment split (taking the whole path suffix instead)
    still passes -- the ``..`` refusal catches it. Breaking both makes the
    ``dot-dot segments`` case serve ``/etc/passwd`` with a 200, which this test
    then fails on. The other five payloads survive either mutation because
    nothing here percent-decodes, so they never become ``..`` at all. So the
    encoded variants document that the surface is closed rather than proving
    it, and the plain one is the case with teeth.

    ``/etc/passwd`` is the target because it exists on this machine; a payload
    aimed at a file that does not exist would 404 for the wrong reason.
    """
    assert Path("/etc/passwd").exists(), "this test's whole premise"
    status, _content_type, body = _get(live_server.base_url, path)
    assert status == 404, (status, body[:200])
    assert b"root:" not in body


def test_media_routes_take_an_id_not_a_path(live_server):
    """The counterpart to the vendor test, and why there is only one traversal
    surface: every thumbnail and original resolves through an integer id looked
    up in the database, so a path in the URL is never opened. A non-numeric id
    cannot reach the filesystem at all -- it fails to parse first.

    The status is asserted loosely (400 from the unparseable id, or 404 if the
    client library collapsed the ``..`` before sending) because which of the two
    arrives depends on urllib, not on this server. The assertion that matters is
    the second one, and it does not care which path got us there.
    """
    status, _content_type, body = _get(live_server.base_url, "/file/../../etc/passwd")
    assert status in (400, 404), (status, body)
    assert b"root:" not in body


def test_a_post_to_a_get_only_route_answers_404(live_server):
    """The two tables are independent, which is what lets ``/api/pet/`` be a GET
    prefix while ``/api/pet/rename`` is a POST. The flip side is that a method
    mismatch is a 404, not a 405 -- pinned because it is a real, deliberate
    consequence of the split rather than an oversight."""
    status, body = _post(live_server.base_url, "/api/summary", {})
    assert status == 404, body


# ---------------------------------------------------------------------------
# GET -- /api routes (28: 24 exact + 4 prefix)
# ---------------------------------------------------------------------------

API_GET_CASES = [
    pytest.param("/api/health", {"ok", "version", "commit"}, id="GET /api/health"),
    pytest.param("/api/archives", {"archives"}, id="GET /api/archives"),
    pytest.param(
        "/api/summary?root={root_id}",
        {"total", "size", "types", "with_gps", "enriched", "date_min", "date_max"},
        id="GET /api/summary",
    ),
    pytest.param("/api/timeline?root={root_id}", {"bucket", "series"}, id="GET /api/timeline"),
    pytest.param(
        "/api/dates/sources?root={root_id}",
        {"total", "dated", "undated", "sources"},
        id="GET /api/dates/sources",
    ),
    pytest.param(
        "/api/map/clusters?root={root_id}", {"clusters", "hidden"}, id="GET /api/map/clusters"
    ),
    pytest.param(
        "/api/map/points?root={root_id}", {"points", "unplaced"}, id="GET /api/map/points"
    ),
    pytest.param(
        "/api/map/cluster/merge-preview?root={root_id}&a={place_a}&b={place_b}",
        {"ok", "span_km", "threshold_km", "warn"},
        id="GET /api/map/cluster/merge-preview (exact)",
    ),
    pytest.param(
        "/api/map/cluster/{place_a}?root={root_id}",
        {"id", "name", "lat", "lon", "total", "members", "offset", "count", "merges"},
        id="GET /api/map/cluster/ (prefix)",
    ),
    pytest.param(
        "/api/faces/summary?root={root_id}",
        {
            "total_images",
            "scanned",
            "unscanned",
            "faces",
            "people",
            "photos_with_faces",
            "backend_available",
        },
        id="GET /api/faces/summary",
    ),
    pytest.param(
        "/api/pets/summary?root={root_id}",
        {
            "total_images",
            "scanned",
            "unscanned",
            "detections",
            "pets",
            "nonhuman_faces",
            "backend_available",
        },
        id="GET /api/pets/summary",
    ),
    pytest.param("/api/pets?root={root_id}", {"pets", "offset", "count"}, id="GET /api/pets"),
    pytest.param(
        "/api/pet/detections?root={root_id}",
        {"items", "offset", "count"},
        id="GET /api/pet/detections",
    ),
    pytest.param(
        "/api/nonhuman?root={root_id}",
        {"items", "total", "offset", "count"},
        id="GET /api/nonhuman",
    ),
    pytest.param(
        "/api/pet/{pet_a}?root={root_id}",
        {"id", "name", "species", "photos", "items", "offset", "count", "merges"},
        id="GET /api/pet/ (prefix)",
    ),
    pytest.param(
        "/api/faces/persons?root={root_id}",
        {"people", "offset", "count"},
        id="GET /api/faces/persons",
    ),
    pytest.param(
        "/api/faces/suggestions?root={root_id}",
        {"suggestions"},
        id="GET /api/faces/suggestions",
    ),
    pytest.param(
        "/api/faces/person/{person_a}?root={root_id}",
        {"id", "name", "photos", "items", "offset", "count", "merges"},
        id="GET /api/faces/person/ (prefix)",
    ),
    pytest.param(
        "/api/dups/summary?root={root_id}",
        {"groups", "duplicates", "reclaimable", "by_match", "by_media"},
        id="GET /api/dups/summary",
    ),
    pytest.param("/api/dups?root={root_id}", {"groups", "offset", "count"}, id="GET /api/dups"),
    pytest.param(
        "/api/media?root={root_id}",
        {"items", "offset", "limit", "count", "total"},
        id="GET /api/media",
    ),
    pytest.param(
        "/api/browse/filters?root={root_id}",
        {"periods", "types", "people", "places", "indexed_any", "located_any"},
        id="GET /api/browse/filters",
    ),
    pytest.param("/api/folders?root={root_id}", {"folders"}, id="GET /api/folders"),
    pytest.param(
        "/api/browse/semantic/status?root={root_id}",
        {"total", "indexed", "skipped", "errors", "pending", "by_type", "configured"},
        id="GET /api/browse/semantic/status",
    ),
    pytest.param(
        "/api/browse/semantic/search?root={root_id}&q=beach",
        {"items", "offset", "limit", "count", "total"},
        id="GET /api/browse/semantic/search",
    ),
    pytest.param(
        "/api/item/{plain}",
        {
            "id",
            "name",
            "type",
            "people",
            "animals",
            "place",
            "place_options",
            "person_options",
            "pet_options",
        },
        id="GET /api/item/ (prefix)",
    ),
    pytest.param(
        "/api/pipeline?root={root_id}",
        {"root_id", "overall", "stages", "extra", "paused", "paused_stages"},
        id="GET /api/pipeline",
    ),
]


@pytest.mark.parametrize("path_template, expected_keys", API_GET_CASES)
def test_every_get_api_route_answers_with_its_shape(live_server, path_template, expected_keys):
    path = path_template.format(**live_server.ids)
    status, _content_type, body = _get(live_server.base_url, path)
    assert status == 200, body
    payload = json.loads(body)
    assert expected_keys <= payload.keys(), payload


def test_get_settings_returns_the_constant_empty_object(live_server):
    """The one route with no shape worth checking by subset -- it always
    returns exactly ``{}`` -- so this gets an exact-equality test instead of
    a slot in API_GET_CASES."""
    status, _content_type, body = _get(live_server.base_url, "/api/settings")
    assert status == 200, body
    assert json.loads(body) == {}


# ---------------------------------------------------------------------------
# POST -- /api routes (19 single-shot cases; 8 more as dedicated tests below
# for routes that mutate destructively or need a multi-request sequence)
# ---------------------------------------------------------------------------

API_POST_CASES = [
    pytest.param(
        "/api/archives",
        lambda ids: {"path": ids["extra_archive_path"]},
        {"id", "path"},
        id="POST /api/archives",
    ),
    pytest.param(
        "/api/archive/open",
        lambda ids: {"root_id": ids["root_id"]},
        {"ok"},
        id="POST /api/archive/open",
    ),
    pytest.param(
        "/api/archive/close",
        lambda ids: {"root_id": ids["root_id"]},
        {"ok"},
        id="POST /api/archive/close",
    ),
    pytest.param(
        "/api/pipeline/pause",
        lambda ids: {"paused": True},
        {"paused"},
        id="POST /api/pipeline/pause (whole)",
    ),
    pytest.param(
        "/api/map/cluster/rename",
        lambda ids: {"cluster_id": ids["place_a"], "name": "Home Renamed"},
        {"ok", "name"},
        id="POST /api/map/cluster/rename",
    ),
    pytest.param(
        "/api/faces/person/rename",
        lambda ids: {"person_id": ids["person_a"], "name": "Ana Renamed"},
        {"ok", "name"},
        id="POST /api/faces/person/rename",
    ),
    pytest.param(
        "/api/faces/reassign",
        lambda ids: {"face_id": ids["face_a"], "person_id": ids["person_b"]},
        {"ok", "person"},
        id="POST /api/faces/reassign",
    ),
    pytest.param(
        "/api/faces/detach",
        lambda ids: {"person_id": ids["person_a"], "file_id": ids["person_a_photo"]},
        {"ok", "detached_faces"},
        id="POST /api/faces/detach",
    ),
    pytest.param(
        "/api/faces/different",
        lambda ids: {"a": ids["person_a"], "b": ids["person_b"]},
        {"ok"},
        id="POST /api/faces/different",
    ),
    pytest.param(
        "/api/faces/skip",
        lambda ids: {"a": ids["person_a"], "b": ids["person_b"]},
        {"ok"},
        id="POST /api/faces/skip",
    ),
    pytest.param(
        "/api/faces/hide",
        lambda ids: {"person_id": ids["person_a"]},
        {"ok"},
        id="POST /api/faces/hide",
    ),
    pytest.param(
        "/api/pet/rename",
        lambda ids: {"pet_id": ids["pet_a"], "name": "Rocco Renamed"},
        {"ok", "name"},
        id="POST /api/pet/rename",
    ),
    pytest.param(
        "/api/nonhuman/review",
        lambda ids: {"detection_id": ids["nonhuman"], "verdict": "confirmed"},
        {"ok", "status", "root_id"},
        id="POST /api/nonhuman/review",
    ),
    pytest.param(
        "/api/item/date",
        lambda ids: {"file_id": ids["plain"], "datetime": "2023-05"},
        {"ok", "date", "date_source"},
        id="POST /api/item/date",
    ),
    pytest.param(
        "/api/item/place",
        lambda ids: {"file_id": ids["plain"], "place_id": ids["place_a"]},
        {"ok", "place"},
        id="POST /api/item/place (set)",
    ),
    pytest.param(
        "/api/item/person/add",
        lambda ids: {"person_id": ids["person_a"], "file_id": ids["plain"]},
        {"ok", "person"},
        id="POST /api/item/person/add",
    ),
    pytest.param(
        "/api/item/person/remove",
        lambda ids: {"person_id": ids["person_a"], "file_id": ids["plain"]},
        {"ok"},
        id="POST /api/item/person/remove",
    ),
    pytest.param(
        "/api/item/pet/add",
        lambda ids: {"pet_id": ids["pet_a"], "file_id": ids["plain"]},
        {"ok", "pet"},
        id="POST /api/item/pet/add",
    ),
    pytest.param(
        "/api/item/pet/remove",
        lambda ids: {"pet_id": ids["pet_a"], "file_id": ids["plain"]},
        {"ok"},
        id="POST /api/item/pet/remove",
    ),
    pytest.param(
        "/api/places/create",
        lambda ids: {"root": ids["root_id"], "name": "New Place", "lat": 1.0, "lon": 2.0},
        {"ok", "id", "place"},
        id="POST /api/places/create",
    ),
]


@pytest.mark.parametrize("path, body_fn, expected_keys", API_POST_CASES)
def test_every_post_api_route_answers_with_its_success_shape(
    live_server, path, body_fn, expected_keys
):
    status, body = _post(live_server.base_url, path, body_fn(live_server.ids))
    payload = json.loads(body)
    assert status == 200, payload
    assert expected_keys <= payload.keys(), payload


# ---------------------------------------------------------------------------
# POST -- dedicated tests: destructive routes, and routes that need a
# multi-request sequence to exercise for real.
# ---------------------------------------------------------------------------


def test_post_archive_remove_deletes_the_registered_archive(live_server):
    ids = live_server.ids
    base = live_server.base_url
    status, body = _post(base, "/api/archive/remove", {"root_id": ids["root_id"]})
    payload = json.loads(body)
    assert status == 200, payload
    assert payload.keys() >= {"ok", "path"}

    # Not just a plausible-looking response: the archive is actually gone.
    _status, _ct, body = _get(base, "/api/archives")
    assert json.loads(body)["archives"] == []


def test_post_pipeline_pause_supports_pausing_a_single_stage(live_server):
    status, body = _post(
        live_server.base_url, "/api/pipeline/pause", {"paused": True, "stage": "scan"}
    )
    payload = json.loads(body)
    assert status == 200, payload
    assert payload.keys() >= {"paused", "paused_stages"}
    assert "scan" in payload["paused_stages"]


def test_post_item_place_clears_an_assignment(live_server):
    """Exercises the ``clear`` branch of /api/item/place; the "set" branch is
    already covered by API_POST_CASES."""
    ids = live_server.ids
    base = live_server.base_url
    status, body = _post(
        base, "/api/item/place", {"file_id": ids["plain"], "place_id": ids["place_a"]}
    )
    assert json.loads(body).get("ok") is True, body

    status, body = _post(base, "/api/item/place", {"file_id": ids["plain"], "clear": True})
    payload = json.loads(body)
    assert status == 200, payload
    assert payload == {"ok": True, "place": None}


def test_post_faces_merge_then_unmerge_round_trips(live_server):
    """merge_persons doesn't hand back the merge_id unmerge needs -- the real
    GUI reads it off the survivor's ``merges`` list (face_person's "merges"
    key) after the merge, exactly like this test does -- so this is the only
    way to exercise /api/faces/unmerge against a genuine merge_id, and
    incidentally the only way to exercise /api/faces/merge with two already
    differently-named clusters (it refuses that without an explicit ``name``,
    see merge_persons' docstring)."""
    ids = live_server.ids
    base = live_server.base_url

    status, body = _post(
        base,
        "/api/faces/merge",
        {"a": ids["person_a"], "b": ids["person_b"], "name": "Merged Person"},
    )
    payload = json.loads(body)
    assert status == 200, payload
    survivor_id = payload["person"]["id"]

    _status, _ct, body = _get(base, f"/api/faces/person/{survivor_id}?root={ids['root_id']}")
    merges = json.loads(body)["merges"]
    assert merges, "expected the merge just made to be listed as undoable"
    merge_id = merges[0]["id"]

    status, body = _post(base, "/api/faces/unmerge", {"merge_id": merge_id})
    payload = json.loads(body)
    assert status == 200, payload
    assert payload == {"ok": True, "recluster": True}


def test_post_pets_merge_then_unmerge_round_trips(live_server):
    """Mirrors test_post_faces_merge_then_unmerge_round_trips; see its
    docstring for why this is a two-request sequence."""
    ids = live_server.ids
    base = live_server.base_url

    status, body = _post(
        base, "/api/pets/merge", {"a": ids["pet_a"], "b": ids["pet_b"], "name": "Merged Pet"}
    )
    payload = json.loads(body)
    assert status == 200, payload
    survivor_id = payload["pet"]["id"]

    _status, _ct, body = _get(base, f"/api/pet/{survivor_id}?root={ids['root_id']}")
    merges = json.loads(body)["merges"]
    assert merges, "expected the merge just made to be listed as undoable"
    merge_id = merges[0]["id"]

    status, body = _post(base, "/api/pets/unmerge", {"merge_id": merge_id})
    payload = json.loads(body)
    assert status == 200, payload
    assert payload == {"ok": True, "recluster": True}


def test_post_map_cluster_merge_then_unmerge_round_trips(live_server):
    """Mirrors test_post_faces_merge_then_unmerge_round_trips. Places are
    durable (place_merges' schema comment), so unlike the face/pet unmerge
    the response carries no ``recluster`` flag -- unmerge_place_clusters is
    already a complete restore, see its docstring."""
    ids = live_server.ids
    base = live_server.base_url

    status, body = _post(
        base,
        "/api/map/cluster/merge",
        {"a": ids["place_a"], "b": ids["place_b"], "name": "Merged Place"},
    )
    payload = json.loads(body)
    assert status == 200, payload
    survivor_id = payload["place"]["id"]

    _status, _ct, body = _get(base, f"/api/map/cluster/{survivor_id}?root={ids['root_id']}")
    merges = json.loads(body)["merges"]
    assert merges, "expected the merge just made to be listed as undoable"
    merge_id = merges[0]["id"]

    status, body = _post(base, "/api/map/cluster/unmerge", {"merge_id": merge_id})
    payload = json.loads(body)
    assert status == 200, payload
    assert payload.keys() == {"ok", "place_id"}
    assert payload["ok"] is True
    assert isinstance(payload["place_id"], int)


# ---------------------------------------------------------------------------
# Drift guard: the routes server.py actually serves must match this file's
# declared route lists.
#
# Routes live in two places while stage 08's rewrite is under way: the tables in
# ``web/routes/`` and whatever is left of do_GET/do_POST's if/elif chains. The
# guard asserts against their *union*, so it stays exact from one end of the
# rewrite to the other -- with the tables empty it is the original chain-scraping
# check, and once the chains are gone it is a plain read of the tables.
# ---------------------------------------------------------------------------

# GET, exact (28: 24 /api + 4 non-api).
GET_EXACT = {
    "/api/health",
    "/api/archives",
    "/api/settings",
    "/api/summary",
    "/api/timeline",
    "/api/dates/sources",
    "/api/map/clusters",
    "/api/map/points",
    "/api/map/cluster/merge-preview",
    "/api/faces/summary",
    "/api/pets/summary",
    "/api/pets",
    "/api/pet/detections",
    "/api/nonhuman",
    "/api/faces/persons",
    "/api/faces/suggestions",
    "/api/dups/summary",
    "/api/dups",
    "/api/media",
    "/api/browse/filters",
    "/api/folders",
    "/api/browse/semantic/status",
    "/api/browse/semantic/search",
    "/api/pipeline",
    "/",
    "/index.html",
    "/manifest.webmanifest",
    "/sw.js",
}
# GET, prefix (11: 4 /api + 7 non-api).
GET_PREFIX = {
    "/api/map/cluster/",
    "/api/pet/",
    "/api/faces/person/",
    "/api/item/",
    "/icon-",
    "/vendor/",
    "/archivethumb/",
    "/thumb/",
    "/faceThumb/",
    "/animalThumb/",
    "/file/",
}
# POST, exact (27, no prefixes).
POST_EXACT = {
    "/api/archives",
    "/api/archive/open",
    "/api/archive/close",
    "/api/archive/remove",
    "/api/pipeline/pause",
    "/api/map/cluster/rename",
    "/api/map/cluster/merge",
    "/api/map/cluster/unmerge",
    "/api/faces/person/rename",
    "/api/faces/reassign",
    "/api/faces/merge",
    "/api/faces/unmerge",
    "/api/faces/detach",
    "/api/faces/different",
    "/api/faces/skip",
    "/api/faces/hide",
    "/api/pet/rename",
    "/api/pets/merge",
    "/api/pets/unmerge",
    "/api/nonhuman/review",
    "/api/item/date",
    "/api/item/place",
    "/api/item/person/add",
    "/api/item/person/remove",
    "/api/item/pet/add",
    "/api/item/pet/remove",
    "/api/places/create",
}


def _route_literals(chunk: str) -> set[str]:
    """Every path string literal a `path` condition compares against, within
    one if/elif chain's source text. Line-scoped (not a whole-file regex) so
    it only picks up literals that are actually part of a route condition."""
    literals: set[str] = set()
    for line in chunk.splitlines():
        if "path ==" in line or "path in (" in line or "path.startswith(" in line:
            literals.update(re.findall(r'"([^"]*)"', line))
    return literals


def _chain_literals() -> tuple[set[str], set[str]]:
    """(GET, POST) route literals still hard-coded in server.py's chains.

    ``do_POST``'s chunk runs to end of file rather than to a named method: the
    methods after it hold no route conditions, and anchoring on one of them
    would make this guard fail for the wrong reason the day it moves.
    """
    source = Path(server.__file__).read_text()
    return (
        _route_literals(source[source.index("def do_GET") : source.index("def do_POST")]),
        _route_literals(source[source.index("def do_POST") :]),
    )


def test_route_literals_match_server_py_exactly():
    """Checks every route server.py serves -- from the ``web/routes`` tables and
    from what remains of the if/elif chains -- against GET_EXACT/GET_PREFIX/
    POST_EXACT above, so a route added or dropped without a matching update here
    fails a test instead of silently drifting out of what this file covers.

    Verified to fail on a planted regression: dropping ``"/api/health"`` from
    GET_EXACT, deleting an elif branch from server.py, or removing an entry from
    ``routes.GET_ROUTES`` each trips this assertion (checked by hand -- there is
    no supported way to mutate the shipped server from within a test)."""
    get_chain, post_chain = _chain_literals()
    get_table = set(server.routes.GET_ROUTES) | {p for p, _ in server.routes.GET_PREFIX_ROUTES}
    post_table = set(server.routes.POST_ROUTES)

    assert get_table | get_chain == GET_EXACT | GET_PREFIX
    assert post_table | post_chain == POST_EXACT
    # Nothing may be served twice: a route left in the chain *and* added to a
    # table would answer from the table, making the chain branch dead code that
    # still reads as live.
    assert not (get_table & get_chain), "route in both the GET table and the GET chain"
    assert not (post_table & post_chain), "route in both the POST table and the POST chain"
