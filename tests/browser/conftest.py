"""The browser tier: a real Chrome, a real server, and the app's own JS.

Everything else in the suite stops at the HTTP boundary. That leaves about
4,000 lines of ES modules -- the router, seven screen renderers, infinite
scroll, drag-to-merge, the search composer -- checked by nothing: `eslint`
grades syntax, `check_handlers.py` proves an inline `on*` names something that
exists, and neither can see a renderer that throws on load or a screen that
paints empty. That is the gap this tier exists for, and it is deliberately
narrow: does each screen actually render, does navigating work, and does the
page raise no errors doing it.

No new dependency. It drives the same stdlib DevTools Protocol client the
screenshot tooling uses (`tools/dev/cdp_shot.py`), so the frontend stays a
plain no-build-step ES module tree (ADR 0002).

**The tier skips itself** when no Chrome can be found or started, so a
contributor without one, and `make test`, are unaffected -- `make test-browser`
is what asks for it deliberately. Set `TROVE_CDP_PORT` to attach to a browser
you started yourself instead of letting the fixture launch one.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import socket
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import factories
import pytest
from helpers import serve_in_thread

from trove import features
from trove.config import Config
from trove.db import database as db
from trove.services import archives

TOOLS_DEV = Path(__file__).resolve().parents[2] / "tools" / "dev"

# One more than the Library grid's own page size (GRID_PAGE_SIZE = 120 in
# static/js/library.js), so there is a second page to fetch and paging can be
# tested at all. Every test pays for these, but they are 32x32 JPEGs -- the
# archive rebuild stays a fraction of the browser start it sits behind. If
# GRID_PAGE_SIZE ever changes, this has to stay above it or the paging test
# silently stops testing paging.
MEDIA_COUNT = 130

_CHROME_BINARIES = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "chrome",
)


BROWSER_DIR = Path(__file__).resolve().parent


def pytest_collection_modifyitems(items):
    """Mark everything in *this directory* ``browser``.

    Applied here rather than as a ``pytestmark`` line per module so a new test
    file cannot forget it: the mark is what keeps ``make test`` from launching
    a browser, and a module that missed it would put a Chrome start into the
    default suite without anyone choosing that.

    The path check is not optional. This hook is global once the conftest is
    loaded -- it receives every collected item, not only the ones under this
    directory -- so marking unconditionally marked the entire suite, and
    ``make test``'s ``-m "not browser"`` then deselected all 636 of them and
    reported success in under a second.
    """
    for item in items:
        if BROWSER_DIR in item.path.parents:
            item.add_marker(pytest.mark.browser)


def _load_cdp():
    """Import cdp_shot.py by path -- the same idiom shoot_all.py uses."""
    spec = importlib.util.spec_from_file_location("cdp_shot", TOOLS_DEV / "cdp_shot.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cdp = _load_cdp()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _endpoint_ready(port: int, timeout: float = 20.0) -> bool:
    """Poll DevTools' own /json/version until the browser answers."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            cdp.http_json(port, "/json/version")
            return True
        except Exception:
            # Connection refused until the browser has bound its port. Any
            # other failure resolves the same way -- keep polling until the
            # deadline, then report "no browser" rather than a stack trace
            # about a socket.
            time.sleep(0.1)
    return False


@pytest.fixture(scope="session")
def cdp_port():
    """A DevTools port to drive, launching a headless Chrome if needed.

    Session-scoped: starting a browser costs about a second, and tabs are the
    per-test unit anyway -- each test opens exactly one and closes it, which is
    what stops the leftover-tab starvation CONTRIBUTING documents.
    """
    existing = os.environ.get("TROVE_CDP_PORT")
    if existing:
        port = int(existing)
        if not _endpoint_ready(port, timeout=2.0):
            pytest.skip(f"TROVE_CDP_PORT={port} set, but nothing is listening there")
        yield port
        return

    binary = next((b for b in _CHROME_BINARIES if shutil.which(b)), None)
    if binary is None:
        pytest.skip("no Chrome/Chromium on PATH (set TROVE_CDP_PORT to use a running one)")

    port = _free_port()
    with tempfile.TemporaryDirectory(prefix="trove-cdp-") as profile:
        proc = subprocess.Popen(
            [
                binary,
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                f"--remote-debugging-port={port}",
                "--remote-debugging-address=127.0.0.1",
                f"--user-data-dir={profile}",
                # A fresh profile pops first-run UI and background fetches that
                # have nothing to do with the app under test.
                "--no-first-run",
                "--disable-extensions",
                "--disable-background-networking",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            if not _endpoint_ready(port):
                pytest.skip(f"{binary} did not open a debugging port within 20s")
            yield port
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def _write_jpeg(path: Path, color: tuple[int, int, int] = (120, 140, 160)) -> None:
    """A real, small, decodable JPEG, so thumbnail serving takes its real path."""
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 32), color).save(path, "JPEG")


def _seed_documents(conn, root_id: int, source_dir: Path, photo_id: int) -> int:
    """Text already read out of two documents *and* one photograph, so Browse
    has a second result group and that group holds more than one kind of file.
    Returns the first document's id.

    The photograph is not decoration. Both text features write into the same
    passages, so this group is mixed on any archive running both -- and
    while the fixture held documents alone, a thumbnail with nothing bounding its
    height went unseen here: it grew to fill the column and stretched every
    document beside it into a cell four times the size of its own text.

    Written straight into the tables rather than run through the stage: what
    this tier checks is the screen, and a real extraction pass would put PDF
    parsing in front of every browser test.
    """
    first = 0
    for name, page, body in (
        # English on purpose: a Spanish query sends the composer through the
        # local translator, which downloads 23 MB on first use. What this tier
        # checks is the screen, not the model behind the other group.
        ("lease.pdf", 2, "The lease agreement was signed in Bariloche"),
        ("receipt.txt", None, "Receipt for the lease, paid in March"),
    ):
        # An explicit hash: doc_text.source_sha256 is NOT NULL, and it is the
        # anchor that says the text matches the bytes it was read from.
        digest = f"sha-{name}"
        fid = factories.add_file(
            conn,
            root_id=root_id,
            rel_path=name,
            media_type="document",
            ext=name.rsplit(".", 1)[1],
            sha256=digest,
        )
        (source_dir / name).write_text(body, encoding="utf-8")
        conn.execute(
            """INSERT INTO doc_text(file_id, source_sha256, wanted, extractor, status,
                                    chars, n_chunks, text_version, extracted_at)
               VALUES(?, ?, 'documents', 'pdf-text', 'extracted', ?, 1, 'doctext-v1', ?)""",
            (fid, digest, len(body), factories.FIXED_TIME),
        )
        cur = conn.execute(
            "INSERT INTO doc_chunks(file_id, ordinal, page_first, page_last, chars) "
            "VALUES(?, 0, ?, ?, ?)",
            (fid, page, page, len(body)),
        )
        conn.execute("INSERT INTO doc_chunk_fts(rowid, text) VALUES(?, ?)", (cur.lastrowid, body))
        first = first or fid

    # ...and the writing read off a photograph. It shares "receipt" with the
    # text file above, so one search can return both kinds of file and the group
    # can be asserted on for holding them at the same size; it deliberately
    # shares nothing with "lease", which is what the searches that count only
    # documents look for.
    photo_text = "RECEIPT / RECIBO DE COMPRA -- TOTAL 43,50 EUR"
    # The seeded photographs carry no hash, and `doc_text.source_sha256` is the
    # anchor saying the text matches the bytes it was read from, so give this
    # one the hash a real scan would have written.
    digest = f"sha-ocr-{photo_id}"
    conn.execute("UPDATE files SET sha256=? WHERE id=?", (digest, photo_id))
    conn.execute(
        """INSERT INTO doc_text(file_id, source_sha256, wanted, extractor, status,
                                chars, n_chunks, text_version, extracted_at)
           VALUES(?, ?, 'ocr', 'ocr', 'extracted', ?, 1, 'doctext-v1', ?)""",
        (photo_id, digest, len(photo_text), factories.FIXED_TIME),
    )
    cur = conn.execute(
        "INSERT INTO doc_chunks(file_id, ordinal, page_first, page_last, chars) "
        "VALUES(?, 0, NULL, NULL, ?)",
        (photo_id, len(photo_text)),
    )
    conn.execute("INSERT INTO doc_chunk_fts(rowid, text) VALUES(?, ?)", (cur.lastrowid, photo_text))

    return first


def _seed(conn, root_id: int, source_dir: Path) -> dict:
    """A small archive with something on every screen.

    Every screen needs content: one rendering its empty state is
    indistinguishable, from the outside, from one whose renderer threw -- and
    "the empty state renders" is not what this tier is checking.
    """
    ids: dict[str, int] = {}

    def _file(rel_path: str, **kw) -> int:
        fid = factories.add_file(conn, root_id=root_id, rel_path=rel_path, **kw)
        _write_jpeg(source_dir / rel_path)
        return fid

    file_ids = []
    for i in range(MEDIA_COUNT):
        fid = _file(f"2024/photo{i:03d}.jpg")
        factories.add_date(conn, fid, best_datetime=f"2024-{1 + i % 9:02d}-15T10:00:00")
        file_ids.append(fid)
    ids["first_file"] = file_ids[0]

    # Two of each: a merge needs a second card to drop onto, so one of anything
    # cannot exercise the interaction at all.
    for name, fid in (("Ada", file_ids[0]), ("Grace", file_ids[1])):
        person_id = factories.add_person(conn, name=name)
        factories.add_face(conn, fid, person_id=person_id)
        conn.execute("UPDATE persons SET face_count=1 WHERE id=?", (person_id,))
        ids[f"person_{name.lower()}"] = person_id
    for name, fid in (("Kira", file_ids[2]), ("Rex", file_ids[3])):
        pet_id = factories.add_pet(conn, name=name)
        factories.add_animal_detection(conn, fid, pet_id=pet_id)
        conn.execute("UPDATE pets SET detection_count=1 WHERE id=?", (pet_id,))
        ids[f"pet_{name.lower()}"] = pet_id

    # Places: geotagged members under one named cluster.
    geotagged = file_ids[:12]
    for fid in geotagged:
        factories.add_geo(conn, fid)
    ids["place"] = factories.add_place(conn, name="Bariloche", root_id=root_id, file_ids=geotagged)

    # One duplicate group, so Duplicates has a row rather than its empty state.
    # A visual match rather than a byte-identical pair -- distinct sha256s, so
    # the copy's tile renders the "Visual match" tag, which is the case with
    # something to draw.
    canonical, copy = file_ids[4], file_ids[5]
    for fid, sha in ((canonical, "d" * 64), (copy, "e" * 64)):
        conn.execute("UPDATE files SET sha256=? WHERE id=?", (sha, fid))
    cur = conn.execute(
        """INSERT INTO dup_groups(method, canonical_file_id, member_count,
                                  size_each, redundant_bytes, created_at)
           VALUES('perceptual', ?, 2, 4, 4, ?)""",
        (canonical, factories.FIXED_TIME),
    )
    ids["dup_group"] = cur.lastrowid
    ids["dup_kept"], ids["dup_copy"] = canonical, copy
    for fid, role in ((canonical, "canonical"), (copy, "duplicate")):
        conn.execute(
            "INSERT INTO dup_members(group_id, file_id, role) VALUES(?, ?, ?)",
            (ids["dup_group"], fid, role),
        )

    # A photograph none of the other fixtures has claimed: not the one the item
    # panel opens, not a person's, not a pet's, not half of the duplicate pair.
    ids["ocr_photo"] = file_ids[20]
    ids["document"] = _seed_documents(conn, root_id, source_dir, ids["ocr_photo"])

    # The run that produced the group above. Dedup writes no per-file row -- a
    # file with no copies is simply in no group -- so this marker is the whole
    # of what says a file has been compared, and the viewer's Duplicates section
    # reads it to tell "no copies" from "not compared yet". Written last, so it
    # covers everything seeded; a group no run ever made is a state no real
    # archive can be in.
    db.dedup_mark_done(conn, root_id, *db.dedup_coverage(conn, root_id))

    # Without this, opening the archive treats every person seeded above as
    # stale identity data from a retired embedder and wipes them -- see the
    # same call in tests/gui/live_archive.py, where it cost a debugging session.
    from trove.faces import backend as face_backend
    from trove.faces import migrate_adaface

    migrate_adaface.mark_embedder(conn, face_backend.EMBEDDER_VERSION)
    return ids


class Archive:
    """One served archive: its base URL, its root id, and the ids seeded in it."""

    def __init__(self, base_url: str, root_id: int, ids: dict, port: int):
        self.base_url = base_url
        self.root_id = root_id
        self.ids = ids
        self.port = port

    def url(self, section: str | None = None) -> str:
        if section is None:
            return f"{self.base_url}/"
        return f"{self.base_url}/#/archive/{self.root_id}/{section}"


@pytest.fixture
def archive(tmp_path, cdp_port):
    """A live server over a seeded archive, plus the CDP port to drive it."""
    pytest.importorskip("PIL.Image")
    cfg = Config.load()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    registered = archives.add_archive(cfg, str(source_dir))
    assert "id" in registered, registered
    root_id = registered["id"]
    # Every feature: what this tier checks is the screen, and a screen drawn
    # from a partial feature set would not be the one a full archive shows.
    cfg.set_archive_features(root_id, list(features.ids()))

    conn = db.connect(cfg.archive_db_path(root_id))
    try:
        ids = _seed(conn, root_id, source_dir)
        conn.commit()
    finally:
        conn.close()
    # The folder this archive *is*, for the start page's own question: whether a
    # chosen folder could become one.
    ids["archive_path"] = str(source_dir)

    with serve_in_thread(cfg) as httpd:
        host, port = httpd.server_address[:2]
        yield Archive(f"http://{host}:{port}", root_id, ids, cdp_port)


VIEWPORT = {"width": 1400, "height": 1000, "deviceScaleFactor": 1, "mobile": False}

# Same hosts shoot_all.py blocks, for a stronger reason here: the map defaults
# to tiles ON, and a test suite that reaches a public tile server breaks the
# project's own "no network calls" rule and fails on an offline machine.
TILE_HOSTS = ["*tile.openstreetmap.org*", "*basemaps.cartocdn.com*"]

# Installed before any page script runs, so it catches a renderer that throws
# during startup -- which is the failure this tier exists to see. Both hooks
# are needed: a throwing module reaches `error`, while an `await` that rejects
# inside a screen renderer only ever surfaces as `unhandledrejection`.
_TRAP_JS = """
window.__errors = [];
addEventListener('error', e => window.__errors.push(String(e.message || e.error)));
addEventListener('unhandledrejection', e => window.__errors.push(
  'unhandled rejection: ' + ((e.reason && e.reason.message) || e.reason)));
localStorage.setItem('navCollapsed', '0');
"""

# What router.js writes into #main while a screen's renderer is awaiting its
# fetches. Matched exactly rather than by class, so if the placeholder ever
# changes, this tier fails loudly at that one string instead of silently
# racing every render.
LOADING_TEXT = "Loading…"


class App:
    """A tab with the app loaded in it, and the vocabulary the tests assert in.

    Thin on purpose: everything here is one line of JS through ``Tab.evaluate``.
    The point is that a test reads as what it is checking rather than as a
    string of DOM expressions.
    """

    def __init__(self, tab):
        self.tab = tab

    def count(self, selector: str) -> int:
        return self.tab.evaluate(f"document.querySelectorAll({selector!r}).length")

    def text(self, selector: str = "#main") -> str:
        return self.tab.evaluate(
            f"(document.querySelector({selector!r}) || {{}}).textContent || ''"
        )

    def wait_for(self, selector: str, timeout: float = 15.0):
        """Block until at least one element matches, naming it on timeout."""
        return self.tab.wait_for(
            f"document.querySelectorAll({selector!r}).length > 0",
            timeout=timeout,
            what=f"an element matching {selector!r}",
        )

    def wait_until_loaded(self, timeout: float = 20.0) -> None:
        """Block until a screen has actually rendered into ``#main``.

        Two states have to be waited out, not one, and missing either makes
        every assertion downstream race the render:

        * **Empty.** The app boots into the picker and only reaches a section
          via ``loadPicker().then(applyHash)``, so for the first moments
          ``#main`` is empty -- which is not the loading placeholder, and a
          check for only that sails straight through it.
        * **The placeholder.** ``showSection`` then puts a single "Loading…"
          node there while the screen's renderer awaits its fetches
          (``static/js/router.js``).

        Requiring non-empty *and* not-the-placeholder covers both. It also
        turns a renderer that throws into a clear failure rather than a
        confusing one: the placeholder is never replaced, so this times out
        naming that possibility.
        """
        self.tab.wait_for(
            "(() => { const t = document.getElementById('main').textContent.trim();"
            f" return t.length > 0 && t !== {LOADING_TEXT!r}; }})()",
            timeout=timeout,
            what="a screen to render into #main (its renderer may have thrown)",
        )

    def click(self, selector: str) -> None:
        self.tab.evaluate(f"document.querySelector({selector!r}).click()")

    def show_section(self, section: str) -> None:
        """Navigate the way the nav does -- through the app's own entry point.

        ``showSection`` is on ``window`` because index.html's inline handlers
        need it there (see main.js's export block), so driving it is using the
        app's real navigation path rather than a test-only backdoor.
        """
        self.tab.evaluate(f"showSection({section!r})")
        self.wait_until_loaded()

    def wait_for_text(self, substring: str, timeout: float = 15.0) -> None:
        """Block until ``#main`` contains ``substring``.

        ``wait_until_loaded`` only proves the placeholder is gone, and several
        screens paint their heading before the list under it arrives -- People
        and Pets fill theirs through ``startInfiniteList``. Asserting on text
        straight after load therefore races the fetch, which is exactly how
        this tier produced its first intermittent failure. Wait for the thing
        being asserted, not for the screen in general.
        """
        self.tab.wait_for(
            f"document.getElementById('main').textContent.includes({substring!r})",
            timeout=timeout,
            what=f"{substring!r} to appear on the screen",
        )

    def wait_until_settled(self, quiet: float = 0.25, timeout: float = 15.0) -> str:
        """Block until ``#main``'s text stops changing, and return it.

        For the cases with no single substring to wait for -- comparing a
        screen against itself across a navigation, say. Two equal readings a
        quarter-second apart is a heuristic, not a proof, but it is the honest
        one available: the app has no "done rendering" signal to observe.
        """
        deadline = time.monotonic() + timeout
        previous = None
        while True:
            current = self.text("#main")
            if current and current == previous:
                return current
            if time.monotonic() >= deadline:
                raise AssertionError(f"#main never settled within {timeout}s")
            previous = current
            time.sleep(quiet)

    def scroll_to_bottom(self) -> None:
        """Scroll ``#main``, which is the scroll container -- not the window."""
        self.tab.evaluate("(m => m.scrollTop = m.scrollHeight)(document.getElementById('main'))")

    def hash(self) -> str:
        return self.tab.evaluate("location.hash")

    def active_nav(self) -> str:
        """The label of the nav item currently marked as the open screen."""
        return self.tab.evaluate(
            "(document.querySelector('#navitems .navitem.active') || {}).title || ''"
        )

    def errors(self) -> list[str]:
        """Uncaught errors and rejections since the page began loading."""
        return self.tab.evaluate("window.__errors") or []


@pytest.fixture
def open_app(archive):
    """Open the app at a section and yield an ``App``, closing the tab after.

    A context manager rather than a plain fixture because the tab must be
    closed even when the test fails: a leftover tab keeps the app's status
    poller running and starves the browser for every later test, which is the
    trap CONTRIBUTING documents against the screenshot tooling.
    """

    @contextmanager
    def _open(section: str | None = None, wait_for: str | None = None):
        with cdp.open_tab(port=archive.port) as tab:
            tab.call("Page.enable")
            tab.call("Runtime.enable")
            tab.call("Emulation.setDeviceMetricsOverride", VIEWPORT)
            tab.call("Network.enable")
            tab.call("Network.setBlockedURLs", {"urls": TILE_HOSTS})
            # about:blank first, then navigate: the trap has to be installed
            # before the app's own startup script runs, and that ordering is
            # only guaranteed by attaching first.
            tab.call("Page.addScriptToEvaluateOnNewDocument", {"source": _TRAP_JS})
            tab.call("Page.navigate", {"url": archive.url(section)})
            app = App(tab)
            # The shell exists before any screen renders; waiting on it first
            # means a later timeout names the screen, not the whole page.
            tab.wait_for("!!document.getElementById('main')", timeout=20.0, what="the app shell")
            if section is not None:
                app.wait_until_loaded()
            if wait_for:
                app.wait_for(wait_for)
            yield app

    return _open
