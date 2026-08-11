"""The Overview draws before its numbers arrive.

Six requests fill this screen, and on a large archive on a cold cache they used
to be the whole wait: the page did not exist until the slowest one answered.
It does not have to be. The picker already handed the client this archive's
file count and byte total -- the very figures on the card the user just clicked
-- and /api/summary answers them with the same two aggregates over the same
rows, so they can be shown at once rather than fetched to be told again.

What the screen must never do is fill the gap with a number it does not have.
The tiles that need a request of their own say so until it lands.
"""

from __future__ import annotations

# Hold /api/summary open so the first paint is observable, then draw the
# Overview again. Every other request is left alone: this is about what the
# screen shows while its own slowest answer is outstanding, not about a broken
# server.
_HOLD_SUMMARY_JS = """
  (() => {
    const real = window.fetch;
    window.__released = false;
    window.fetch = (u, o) => (String(u).includes('/api/summary')
      ? new Promise(resolve => {
          window.__release = () => { window.__released = true; resolve(real(u, o)); };
        })
      : real(u, o));
    // `true` is showSection's reload flag: without it the call is a no-op,
    // because the Overview is already the active section (router.js:171) and
    // the screen under test would never be drawn a second time.
    showSection('overview', true);
    return true;
  })()
"""

_LOADED = "!document.querySelector('.statrow .stat-unknown')"
_TILE = "document.getElementById('{}').textContent"


def test_the_file_count_is_on_screen_before_the_summary_answers(open_app):
    """The number the picker already knew, shown while the request that would
    confirm it is still in flight."""
    with open_app("overview") as app:
        # The settled answer, read off the screen once every tile has one. That
        # is what /api/summary said, so it is exactly what the paint below has
        # to match without asking for it again.
        app.tab.wait_for(_LOADED, timeout=15.0, what="the Overview to finish loading")
        expected = app.tab.evaluate(_TILE.format("ov-total")).strip()
        assert expected not in ("", "0"), f"fixture archive has no files to count: {expected!r}"

        app.tab.evaluate(_HOLD_SUMMARY_JS)
        app.wait_for("#ov-total")
        shown = app.tab.evaluate(_TILE.format("ov-total")).strip()
        held = app.tab.evaluate("window.__released")

        assert held is False, "/api/summary answered before the assertion; the test is void"
        assert shown == expected, f"expected the picker's count {expected!r}, got {shown!r}"
        app.tab.evaluate("window.__release()")


def test_a_tile_with_nothing_to_show_yet_does_not_show_a_zero(open_app):
    """Zero duplicates and "not counted yet" are different claims, and this
    screen is the one place in the app whose whole job is to say which is
    which. The placeholder holds the slot; it never stands in for a figure."""
    with open_app("overview") as app:
        app.tab.wait_for(_LOADED, timeout=15.0, what="the Overview to finish loading")
        app.tab.evaluate(_HOLD_SUMMARY_JS)
        app.wait_for("#ov-total")
        # Read straight after the paint, while the tile's own answer is still
        # outstanding. If the placeholder rendered "0" instead, a zero was
        # briefly on screen and this catches it.
        dated = app.tab.evaluate(_TILE.format("ov-enriched")).strip()

        assert dated != "0", "an uncounted tile rendered a zero"
        app.tab.evaluate("window.__release()")


def test_every_tile_holds_a_real_number_once_the_answers_land(open_app):
    """The other half of the same rule: a placeholder that never resolves is
    worse than the wait it replaced."""
    with open_app("overview") as app:
        app.wait_for("#ov-total")
        app.tab.wait_for(_LOADED, timeout=15.0, what="every Overview tile to be filled in")
        assert app.errors() == [], f"the Overview raised: {app.errors()}"


# The whole screen is rendered from one snapshot, so a state that only the
# server can produce is best injected there: overwrite the field the renderer
# reads and redraw, rather than renaming a folder out from under a live archive.
_WITH_SNAPSHOT_JS = """
  (() => {
    const patch = %s;
    const real = window.fetch;
    window.fetch = async (u, o) => {
      const res = await real(u, o);
      if (!String(u).includes('/api/pipeline?')) return res;
      const body = await res.clone().json();
      return new Response(JSON.stringify({ ...body, ...patch }),
        { headers: { 'Content-Type': 'application/json' } });
    };
    showSection('overview', true);
  })()
"""


def test_a_missing_folder_is_said_out_loud_rather_than_shown_as_healthy(open_app):
    """The regression: an archive whose folder cannot be reached reported green
    dots, "up to date" and a full file count, while every thumbnail and original
    in it answered 404. The notice leads the health panel, the sidebar chip says
    so from every screen, and the figures -- which are now facts about the
    catalogue rather than about the archive -- stop looking current."""
    with open_app("overview", wait_for=".health-task") as app:
        app.tab.evaluate(_WITH_SNAPSHOT_JS % "{ root_missing: true }")
        app.wait_for(".health-missing")

        notice = app.text(".health-missing")
        assert "cannot be found" in notice
        assert "thumbnails and originals will not open" in notice
        # Not an error, and not a dead end: it says what to do about it.
        assert "connect it" in notice

        assert "Folder not found" in app.text("#gstat")
        assert app.count(".statrow .stat.stat-stale") == app.count(".statrow .stat"), (
            "every headline figure is unverifiable while the folder is unreadable"
        )
        assert app.errors() == []


def test_one_paused_step_is_not_reported_as_the_whole_pipeline(open_app):
    """The chip used to read `overall`, which says "paused" whenever the only
    outstanding work sits behind one stopped stage -- so pausing a single step
    put a flat "Paused" over an archive that was busy indexing, next to a button
    still offering to "Pause all"."""
    with open_app("overview", wait_for=".health-task") as app:
        app.tab.evaluate(
            _WITH_SNAPSHOT_JS % '{ paused: false, paused_stages: ["dedup"], overall: "paused" }'
        )
        app.tab.wait_for(
            "document.getElementById('gstat').textContent.includes('paused')",
            what="the chip to report the stopped step",
        )

        assert "1 step paused" in app.text("#gstat")
        assert app.text("#pause-btn").strip() == "Pause all", "the pipeline is not paused"
        assert app.errors() == []


def test_the_stat_row_fills_its_width_whatever_the_archive_runs(open_app):
    """Places is optional, so the row is three tiles as often as four. Fixed at
    four columns, three of them left the row ending short of the page head and
    both panels below it."""
    with open_app("overview", wait_for=".statrow .stat") as app:
        # The fixture archive runs every feature, so it draws all four tiles and
        # a four-column grid fits it exactly -- the case that was broken is the
        # ordinary one, an archive without Places. Dropping a tile is how that
        # archive's row is reached from here.
        gaps = app.tab.evaluate("""
          (() => {
            const row = document.querySelector('.statrow');
            const measure = () => {
              const r = row.getBoundingClientRect();
              const last = [...row.querySelectorAll('.stat')].pop().getBoundingClientRect();
              return Math.round(r.right - last.right);
            };
            const before = measure();
            row.querySelectorAll('.stat')[2].remove();
            return [before, measure(), row.querySelectorAll('.stat').length];
          })()
        """)
        four, three, left = gaps

        assert left == 3, "expected three tiles after dropping one"
        assert four < 4, f"a full row already stops {four}px short"
        assert three < 4, f"a three-tile row stops {three}px short of the page"
        assert app.errors() == []
