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
