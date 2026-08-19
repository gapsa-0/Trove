"""Moving between screens: the router, the stash, and the modals.

`router.js` is the one module every screen depends on and nothing else tests.
It does three separable things -- switch sections, keep the URL and nav in
step, and stash a screen's DOM so returning to it does not re-fetch -- and each
fails silently in its own way: a section that switches without updating the
hash breaks reload and back; a stash that does not resume shows a blank screen
where content used to be.

Driven through `showSection`, `openItem` and `openSettings`, which are on
`window` because index.html's inline handlers need them there (main.js's export
block). That is the app's own navigation path, not a test-only entry.
"""

from __future__ import annotations


def test_switching_section_updates_the_url_and_the_nav(open_app, archive):
    """All three have to move together, and the hash is the one that matters
    beyond the session: it is what a reload and the desktop shell restore."""
    with open_app("overview") as app:
        assert app.hash() == f"#/archive/{archive.root_id}/overview"
        assert app.active_nav() == "Overview"

        app.show_section("timeline")

        assert app.hash() == f"#/archive/{archive.root_id}/timeline"
        assert app.active_nav() == "Timeline"
        assert app.errors() == []


def test_a_screen_is_still_there_after_leaving_and_coming_back(open_app):
    """The stash/resume path (`SECTION_VIEWS`), which has no other coverage.

    A screen the user returns to is re-attached from a saved fragment rather
    than re-rendered, so a bug here does not throw -- it shows an empty screen
    where content used to be, which is exactly the failure nothing else notices.
    """
    with open_app("timeline") as app:
        before = app.wait_until_settled()
        assert len(before.strip()) > 40

        app.show_section("dups")
        app.show_section("timeline")

        assert app.wait_until_settled().strip() == before.strip()
        assert app.errors() == []


def test_visiting_every_screen_in_one_tab_raises_nothing(open_app):
    """Teardown, which only a sequence can reach.

    Each screen leaves something running -- a status poller, a Leaflet map, an
    IntersectionObserver -- and `showSection` is what stops them. A leak or a
    double-dispose shows up when one screen follows another, never when each is
    loaded into a fresh tab, so this walks all seven in one.
    """
    with open_app("overview") as app:
        for section in ("library", "timeline", "people", "pets", "places", "dups", "overview"):
            app.show_section(section)
        assert app.errors() == []


def test_opening_an_item_shows_the_viewer(open_app, archive):
    with open_app("library") as app:
        app.tab.evaluate(f"openItem({archive.ids['first_file']})")
        app.wait_for("#modal.open")
        assert app.errors() == []


def test_the_settings_drawer_opens_and_closes(open_app):
    with open_app("overview") as app:
        app.tab.evaluate("openSettings()")
        app.wait_for("#settings-drawer.open")
        app.tab.evaluate("closeSettings()")
        app.tab.wait_for(
            "!document.getElementById('settings-drawer').classList.contains('open')",
            what="the settings drawer to close",
        )
        assert app.errors() == []


def test_the_library_grid_pages_in_more_media_when_scrolled(open_app):
    """Paging, at the seam where it actually breaks.

    The grid loads 120 tiles (`GRID_PAGE_SIZE`) and fetches the rest as the
    bottom comes into view. The archive is seeded with one more than a page
    (`conftest.MEDIA_COUNT`) so a second fetch is required: without it this
    test passes on the first page alone and proves nothing about paging.
    """
    with open_app("library") as app:
        app.wait_for("#main img")
        first_page = app.tab.wait_for(
            "document.querySelectorAll('#main img').length >= 120"
            " && document.querySelectorAll('#main img').length",
            timeout=20.0,
            what="the grid's first page of 120 tiles",
        )
        app.scroll_to_bottom()
        app.tab.wait_for(
            f"document.querySelectorAll('#main img').length > {first_page}",
            timeout=20.0,
            what="a second page of tiles after scrolling",
        )
        assert app.errors() == []


def test_the_collapsed_sidebar_fits_the_rail_it_collapses_to(open_app):
    """Nothing in the rail may be wider than the rail.

    ``nav`` scrolls its own overflow, so one label that does not shrink is not
    a clipped label -- it is a horizontal scrollbar down the side of the
    sidebar, on every screen. Asserted as the overflow rather than on any one
    control, because the next control added to the rail should fail this too.
    """
    with open_app("overview") as app:
        app.tab.evaluate("toggleNav()")
        app.tab.wait_for(
            "document.getElementById('nav').classList.contains('collapsed')",
            timeout=5.0,
            what="the sidebar to collapse",
        )
        widths = app.tab.evaluate(
            "(() => { const n = document.getElementById('nav');"
            " return [n.scrollWidth, n.clientWidth]; })()"
        )
        assert widths[0] <= widths[1], (
            f"the collapsed rail overflows: {widths[0]}px of content in {widths[1]}px"
        )
        assert app.errors() == []


def test_every_way_back_is_the_same_control(open_app):
    """Four screens offer a way back, and three of them used to draw it
    differently -- two with a stroked chevron, two with a typed "←" at a
    different weight that took none of the icon sizing the rest of the app
    shares.

    Asserted as "they are the same element with the same mark", not as a
    screenshot: what matters is that a fifth one added tomorrow is this control
    rather than a fifth drawing of it.
    """
    with open_app("people") as app:
        app.wait_for("#peoplegrid .pcard")

        def marks(where):
            return app.tab.evaluate(
                f"[...document.querySelectorAll({where!r})].map(b => ({{"
                " svg: b.querySelectorAll('svg').length,"
                " text: b.textContent.trim(),"
                " named: !!b.getAttribute('aria-label') }))"
            )

        # The sidebar's, which is static markup in the shell.
        assert marks("nav .back-control") == [{"svg": 1, "text": "", "named": True}]

        app.click(".pcard img.face, .pcard .facecollage img")
        app.wait_for(".facetopbar .back-control")
        assert marks(".facetopbar .back-control") == [{"svg": 1, "text": "", "named": True}]
        assert app.errors() == []


def test_the_collapsed_rail_keeps_the_way_out_of_the_archive(open_app):
    """It is the only one. As a row of text it did not fit the rail and was
    hidden there, which left a collapsed sidebar with no way back to the
    archives at all; as an icon it fits."""
    with open_app("overview") as app:
        app.tab.evaluate("toggleNav()")
        app.tab.wait_for(
            "document.getElementById('nav').classList.contains('collapsed')",
            timeout=5.0,
            what="the sidebar to collapse",
        )
        shown = app.tab.evaluate(
            "(() => { const b = document.querySelector('nav .back-control');"
            " return !!b && getComputedStyle(b).display !== 'none'; })()"
        )
        assert shown, "collapsing the sidebar hid the only way back to the archives"
