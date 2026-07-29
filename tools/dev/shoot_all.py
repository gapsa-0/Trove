#!/usr/bin/env python3
"""Screenshot every GUI screen, in both themes, as a refactor guardrail.

The GUI is server-rendered SQLite reads plus a big single-file `index.html`
front end (see CLAUDE.md "Architecture"). A refactor of the query layer or
the HTML/JS can silently break a screen -- an exception swallowed by a
`.catch`, a renderer that now throws on load, a route that stopped matching
-- and nothing in the test suite looks at the *rendered pixels*. This tool
is the guardrail: shoot every screen before a refactor, shoot them again
after, and diff the two sets. What it is hunting is "a screen that is now
blank or mangled", which shows up as a large pixel delta. Small deltas
(antialiasing, a clock in the corner, a different thumbnail order) are
expected and must not be treated as failures -- see the threshold note
below.

Usage -- two modes:

    # shoot: capture every route, both themes, into out_dir
    python3 tools/dev/shoot_all.py <base_url> <out_dir> <archive_id> \\
        [--port 9333] [--only overview,people] [--tiles]

    # compare: diff two previously-shot directories
    python3 tools/dev/shoot_all.py --compare <dir_a> <dir_b>

Shoot requires a headless Chrome ALREADY RUNNING with a debugging port, e.g.:

    chromium-browser --headless=new --disable-gpu --no-sandbox \\
      --remote-debugging-port=9333 --remote-debugging-address=127.0.0.1 about:blank

and a GUI server already serving `base_url` (`oa gui --port <port>`, or
`python3 -m organize_archive.cli gui --port <port> --no-open`). This tool
does not start either one -- it only drives the DevTools Protocol, reusing
the stdlib-only WS client in `cdp_shot.py` (imported by path; see
`_load_cdp_shot` below) rather than reimplementing it.

Compare threshold: a pair is flagged as a regression when either (a) the
percentage of differing pixels exceeds 5%, or (b) it exceeds 1% AND the mean
absolute difference (MAD, 0-255 per channel) among those pixels-that-differ
exceeds 40. Rationale: a genuinely blank/mangled screen replaces most of the
frame (large area, e.g. a solid background where a grid used to be), while
UI noise -- antialiasing, a re-flowed thumbnail grid, a moving map tile --
touches a real fraction of pixels too but by only a few levels each. The
two-part rule catches "small area, big change" (a single element replaced
by an error box) as well as "big area, big change" (the whole screen gone),
while letting "big area, tiny change" (font hinting, a redrawn map) through.
It is a heuristic, not a proof; read the sorted list, the worst offenders
are worst for a reason.

Measured noise floor (two runs, unchanged tree, 25-file fixture archive):
`overview` `timeline` `people` `pets` `places` `dups` `settings` `picker` come
back **identical**, so any delta on those is worth investigating. `library`
and `item` do NOT settle: their thumbnails are native `loading="lazy"`
(index.html) with `onerror="this.remove()"`, so which tiles have painted at
capture time varies run to run -- 2-20% of pixels, concentrated in the lower
rows. Treat those two as eyeball-only; read the crop, do not trust their exit
code. Everything else is a real signal.

Two traps already burned once fixed here (see CLAUDE.md "Verifying GUI
changes"):

1. Every tab left open keeps polling the server (the GUI runs a job-status
   `setInterval`); a handful of leftover tabs starves the browser and makes
   `Page.captureScreenshot` itself start timing out. Every route shot here
   opens exactly one tab, closes it in a `finally`, and routes are shot
   strictly SEQUENTIALLY -- never open a second tab before the first is
   closed.
2. `--virtual-time-budget` and `--screenshot=` are both broken for this app
   (the former hangs forever against the job-poll `setInterval`, the latter
   snapshots at `load`, before the page's own `fetch()` calls resolve, so it
   captures the empty shell). The only thing that works is a plain
   real-wall-clock `time.sleep()` after navigation, sized per route -- see
   ROUTES below.
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import sys
import time
import urllib.request
from pathlib import Path

TOOLS_DEV = Path(__file__).resolve().parent


def _load_cdp_shot():
    """Import cdp_shot.py by path (it is not a package) -- same idiom as
    tools/build/adaface_export.py loading its sibling adaface_net.py."""
    spec = importlib.util.spec_from_file_location(
        "cdp_shot", TOOLS_DEV / "cdp_shot.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cdp = _load_cdp_shot()

# Seeded into localStorage before the app's own startup script runs, so the
# theme and sidebar state are deterministic for every shot regardless of
# whatever a previous manual session left behind.
_SEED_JS = (
    "localStorage.setItem('archiveTheme', {theme!r});"
    "localStorage.setItem('navCollapsed', '0');"
)

VIEWPORT = {"width": 1400, "height": 1200, "deviceScaleFactor": 1, "mobile": False}

# Map tiles come from public tile servers (see MAP_TILE_STYLES in index.html).
# They arrive over the network at whatever pace the network feels like, so two
# runs of the same code produce visibly different Places screens -- in testing
# that alone was a 1.7% / MAD 128 "regression" on an unchanged tree, which is
# exactly the kind of false alarm that gets a guardrail ignored. Blocking them
# at the protocol level makes the basemap reliably empty while leaving the app
# untouched (no behaviour change, no toggle to remember). The place markers and
# every other pixel of the screen still render, which is what is being guarded.
# It also means no photo coordinates leave the machine during a shoot.
# Pass --tiles when you actually want to look at the basemap.
TILE_HOSTS = ["*tile.openstreetmap.org*", "*basemaps.cartocdn.com*"]

# One row per shootable screen. `wait` is real wall-clock seconds after
# navigation before the screenshot is taken -- local SQLite reads settle in a
# couple of seconds, but Places pulls in map tiles + Leaflet's own layout
# pass, so it gets the most. `hash` is the route fragment (None for the bare
# picker, which has no hash). `post` is an optional JS expression run via
# Runtime.evaluate AFTER the base route has loaded and settled, for the four
# screens that only exist behind a JS call, not a URL -- `post_wait` is the
# extra settle time after that call. `discover` names an id-discovery step
# (see discover_ids()); routes needing one are skipped with a printed note
# when the archive has no such row.
#
# The grid/thumbnail-heavy rows (people, pets, person, pet, and settings --
# which renders on top of the overview route) carry extra margin on top of
# what a quiet archive would need: on a real archive the background pipeline
# (detect/semantic -- see gui/pipeline.py) runs concurrently and competes for
# CPU with thumbnail generation, so on a busy archive these can still show a
# "Loading..." placeholder at 3s where 5-6s is clean. Pause the pipeline
# (Overview -> "Pause all") before a before/after run for the least noise.
ROUTES = [
    {"name": "picker", "hash": None, "wait": 3.0},
    {"name": "overview", "hash": "overview", "wait": 4.0},
    {"name": "library", "hash": "library", "wait": 6.0},
    {"name": "timeline", "hash": "timeline", "wait": 3.0},
    {"name": "people", "hash": "people", "wait": 5.0},
    {"name": "pets", "hash": "pets", "wait": 6.0},
    {"name": "places", "hash": "places", "wait": 6.0},
    {"name": "dups", "hash": "dups", "wait": 4.0},
    {"name": "settings", "hash": "overview", "wait": 4.0,
     "post": "openSettings()", "post_wait": 2.0},
    {"name": "person", "hash": "people", "wait": 5.0,
     "post": "showPerson({id})", "post_wait": 4.0, "discover": "person"},
    # The item modal loads the FULL-SIZE original, not a thumbnail, so it needs
    # far more settle time than the grid behind it: at post_wait 2.0 two runs of
    # an unchanged tree differed by 30% of pixels, purely because the image had
    # not painted yet in one of them.
    {"name": "item", "hash": "library", "wait": 4.0,
     "post": "openItem({id})", "post_wait": 6.0, "discover": "item"},
    {"name": "pet", "hash": "pets", "wait": 5.0,
     "post": "showPet({id})", "post_wait": 4.0, "discover": "pet"},
]


def discover_ids(base_url, archive_id):
    """Look up one real id per data-dependent screen by curling the running
    server -- the ids are archive-specific, so they cannot be hardcoded.
    Missing rows come back as None; callers skip that route with a note
    rather than shooting a route with a bogus id."""
    def jget(path):
        with urllib.request.urlopen(f"{base_url}{path}", timeout=15) as r:
            return json.loads(r.read())

    ids = {}

    people = jget(f"/api/faces/persons?root={archive_id}&limit=1").get("people") or []
    ids["person"] = people[0]["id"] if people else None

    pets = jget(f"/api/pets?root={archive_id}&limit=1").get("pets") or []
    ids["pet"] = pets[0]["id"] if pets else None

    items = jget(f"/api/media?root={archive_id}&limit=1").get("items") or []
    ids["item"] = items[0]["id"] if items else None

    return ids


class Shooter:
    """One CDP session used to shoot every route, one tab at a time."""

    def __init__(self, port, block_tiles=True):
        self.port = port
        self.block_tiles = block_tiles

    def _call(self, sock, ws, msgid_box, method, params=None):
        msgid_box[0] += 1
        mid = msgid_box[0]
        cdp.ws_send_text(sock, json.dumps(
            {"id": mid, "method": method, "params": params or {}}))
        while True:
            obj = json.loads(ws.recv_message())
            if obj.get("id") == mid:
                return obj

    def _settle_images(self, sock, ws, msgid_box, cap=8.0, poll=0.4, stable_polls=3):
        """Block until the page's images stop arriving, or `cap` seconds.

        A fixed sleep is the only thing that works for the app's own fetches
        (see trap 2), but it is a bad fit for images: the thumbnails low in a
        grid land whenever they land, and capturing mid-flight was the single
        biggest source of false "regressions" here -- two runs of an unchanged
        tree differed by 20%+ of pixels, always in the rows below the fold.

        Waiting for "no image is pending" is not enough on its own, because a
        lazily-loaded image below the viewport never gets a src and so stays
        pending forever -- that made every shot pay the full cap and doubled
        the runtime. So this settles on the pending COUNT going quiet: zero
        pending, or the same non-zero count `stable_polls` times running.
        Returns the seconds waited, or None if it gave up at the cap."""
        expr = "Array.from(document.images).filter(i => !i.complete).length"
        waited, last, repeats = 0.0, None, 0
        while waited < cap:
            res = self._call(sock, ws, msgid_box, "Runtime.evaluate",
                             {"expression": expr, "returnByValue": True})
            pending = res.get("result", {}).get("result", {}).get("value")
            if pending == 0:
                return waited
            repeats = repeats + 1 if pending == last else 0
            if repeats >= stable_polls:
                return waited
            last = pending
            time.sleep(poll)
            waited += poll
        return None

    def shoot(self, url, theme, outfile, wait, post=None, post_wait=0.0):
        """Open one tab, seed the theme before any page script runs, navigate,
        settle, optionally run a JS call for the four click-only screens,
        settle again, capture, and ALWAYS close the tab -- a leftover tab
        left polling is what starved Page.captureScreenshot last time this
        was debugged (see module docstring, trap 1)."""
        # Opened as about:blank (no url query) rather than navigating
        # straight to the target: Page.addScriptToEvaluateOnNewDocument must
        # be installed before the app's own startup script reads
        # localStorage, and that race is only safely avoidable by attaching
        # first and navigating second.
        tab = cdp.http_json(self.port, "/json/new", method="PUT")
        tab_id = tab["id"]
        sock = cdp.ws_connect(tab["webSocketDebuggerUrl"])
        ws = cdp.WS(sock)
        msgid_box = [0]
        try:
            self._call(sock, ws, msgid_box, "Page.enable")
            self._call(sock, ws, msgid_box, "Emulation.setDeviceMetricsOverride", VIEWPORT)
            if self.block_tiles:
                self._call(sock, ws, msgid_box, "Network.enable")
                self._call(sock, ws, msgid_box, "Network.setBlockedURLs",
                           {"urls": TILE_HOSTS})
            self._call(sock, ws, msgid_box, "Page.addScriptToEvaluateOnNewDocument",
                       {"source": _SEED_JS.format(theme=theme)})
            self._call(sock, ws, msgid_box, "Page.navigate", {"url": url})
            time.sleep(wait)
            self._settle_images(sock, ws, msgid_box)
            if post:
                self._call(sock, ws, msgid_box, "Runtime.evaluate", {"expression": post})
                time.sleep(post_wait)
                self._settle_images(sock, ws, msgid_box)
            res = self._call(sock, ws, msgid_box, "Page.captureScreenshot", {"format": "png"})
            data = base64.b64decode(res["result"]["data"])
            outfile.write_bytes(data)
            return len(data)
        finally:
            sock.close()
            try:
                cdp.http_json(self.port, f"/json/close/{tab_id}")
            except Exception:
                pass


def cmd_shoot(args):
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    only = set(args.only.split(",")) if args.only else None

    routes = [r for r in ROUTES if only is None or r["name"] in only]

    ids = {}
    needs_discovery = {r["discover"] for r in routes if r.get("discover")}
    if needs_discovery:
        ids = discover_ids(args.base_url, args.archive_id)

    shooter = Shooter(args.port, block_tiles=not args.tiles)
    written, skipped = 0, 0
    for route in routes:
        post = route.get("post")
        item_id = None
        if route.get("discover"):
            item_id = ids.get(route["discover"])
            if item_id is None:
                print(f"skip {route['name']}: archive has no {route['discover']} to open")
                skipped += 2  # both themes
                continue
            post = post.format(id=item_id)

        url = args.base_url if route["hash"] is None else f"{args.base_url}/#/archive/{args.archive_id}/{route['hash']}"

        for theme in ("light", "dark"):
            outfile = out_dir / f"{route['name']}-{theme}.png"
            try:
                size = shooter.shoot(url, theme, outfile, route["wait"],
                                     post=post, post_wait=route.get("post_wait", 0.0))
                print(f"{outfile}  {size} bytes")
                written += 1
            except Exception as e:
                print(f"FAILED {outfile}: {e}")
                skipped += 1

    print(f"\n{written} written, {skipped} skipped, out of {2 * len(routes)} attempted")


# -- compare -----------------------------------------------------------------

# See module docstring "Compare threshold" for the reasoning behind these two
# numbers together.
REGRESSION_PCT_ALONE = 5.0     # differing-pixel share alone is enough above this
REGRESSION_PCT_WITH_MAD = 1.0  # ...or a smaller share, if it's also this severe:
REGRESSION_MAD = 40.0          # mean abs diff (0-255) among the differing pixels


def _compare_pair(path_a, path_b):
    """Returns (verdict_str, is_regression, severity). `severity` is only for
    ordering the report worst-first: a size mismatch is the most severe thing
    that can happen short of the file vanishing, so it sorts above any
    percentage, and an identical pair sorts below every percentage. Imports
    Pillow lazily so plain shooting works without it installed."""
    from PIL import Image
    import numpy as np

    img_a, img_b = Image.open(path_a), Image.open(path_b)
    if img_a.size != img_b.size:
        return f"SIZE MISMATCH {img_a.size} vs {img_b.size}", True, float("inf")

    a = np.asarray(img_a.convert("RGB"), dtype="int16")
    b = np.asarray(img_b.convert("RGB"), dtype="int16")
    diff = np.abs(a - b).sum(axis=2)  # per-pixel sum over channels
    differing = diff > 0
    pct = 100.0 * differing.sum() / differing.size
    if not differing.any():
        return "identical", False, -1.0
    mad = diff[differing].mean() / 3.0  # back to a per-channel 0-255 scale
    is_regression = (pct > REGRESSION_PCT_ALONE or
                      (pct > REGRESSION_PCT_WITH_MAD and mad > REGRESSION_MAD))
    verdict = f"{pct:.1f}% of pixels differ (MAD {mad:.1f})"
    if is_regression:
        verdict += "  <-- REGRESSION"
    return verdict, is_regression, pct


def cmd_compare(args):
    dir_a, dir_b = Path(args.dir_a), Path(args.dir_b)
    names_a = {p.name for p in dir_a.glob("*.png")}
    names_b = {p.name for p in dir_b.glob("*.png")}

    only_a = sorted(names_a - names_b)
    only_b = sorted(names_b - names_a)
    for name in only_a:
        print(f"ONLY IN {dir_a}: {name}  <-- REGRESSION (route stopped rendering?)")
    for name in only_b:
        print(f"ONLY IN {dir_b}: {name}  <-- new route, or route stopped rendering in A")

    results = []
    for name in sorted(names_a & names_b):
        verdict, is_regression, severity = _compare_pair(dir_a / name, dir_b / name)
        results.append((severity, name, verdict, is_regression))

    # Worst-first, so the eye lands on the biggest change: size mismatches,
    # then descending share of differing pixels, then the identical pairs.
    results.sort(key=lambda r: -r[0])
    for _, name, verdict, _ in results:
        print(f"{name}: {verdict}")

    any_regression = bool(only_a or only_b) or any(r[3] for r in results)
    return 1 if any_regression else 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--compare", action="store_true", help="compare two shot directories")
    ap.add_argument("args", nargs="*", help="positional args for the chosen mode")
    ap.add_argument("--port", type=int, default=9333, help="Chrome remote-debugging port")
    ap.add_argument("--only", help="comma-separated route names to shoot, e.g. overview,people")
    ap.add_argument("--tiles", action="store_true",
                    help="allow map tiles to load (default: blocked, for reproducibility)")
    parsed = ap.parse_args()

    if parsed.compare:
        if len(parsed.args) != 2:
            ap.error("--compare needs exactly two directories: --compare <dir_a> <dir_b>")
        ns = argparse.Namespace(dir_a=parsed.args[0], dir_b=parsed.args[1])
        sys.exit(cmd_compare(ns))
    else:
        if len(parsed.args) != 3:
            ap.error("shoot needs exactly: <base_url> <out_dir> <archive_id>")
        base_url, out_dir, archive_id = parsed.args
        ns = argparse.Namespace(
            base_url=base_url.rstrip("/"), out_dir=out_dir, archive_id=int(archive_id),
            port=parsed.port, only=parsed.only, tiles=parsed.tiles)
        cmd_shoot(ns)


if __name__ == "__main__":
    main()
