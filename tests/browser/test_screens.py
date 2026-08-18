"""Every screen renders, and nothing throws doing it.

The narrowest useful question this tier can ask, and the one nothing else in
the suite can: the route tests prove the API answers, but a screen whose
renderer throws on load, or paints an empty shell over a good response, returns
200 for every request it makes and looks perfect from the server's side.

Two assertions per screen, and the second is the one that finds things: the
screen has content, *and* the page recorded no uncaught error or rejection
while producing it. A `.catch` that swallows a real failure still shows up
here, because the screen it was supposed to fill stays empty.
"""

from __future__ import annotations

import importlib.util
import re

import pytest

SECTIONS = ["overview", "library", "timeline", "people", "pets", "places", "dups"]

# The People screen refuses to list anyone without OpenCV's DNN face module,
# showing a "needs the media extra" panel instead -- correct behaviour, and
# the render test above covers that panel, but it means the *listing* can only
# be asserted where the extra is installed. Pets has no such preflight, which
# is why only this one is guarded.
needs_face_detection = pytest.mark.skipif(
    importlib.util.find_spec("cv2") is None,
    reason="the People screen gates its listing on OpenCV (the 'faces' extra)",
)


@pytest.mark.parametrize("section", SECTIONS)
def test_every_screen_renders_content_without_throwing(open_app, section):
    with open_app(section) as app:
        # Long enough to be a rendered screen rather than a spinner or a
        # heading on its own; short enough not to encode any screen's wording.
        assert len(app.text("#main").strip()) > 40, f"{section} rendered almost nothing"
        assert app.errors() == [], f"{section} raised: {app.errors()}"


def test_the_library_grid_fills_with_the_archives_media(open_app):
    with open_app("library") as app:
        app.wait_for("#main img")
        assert app.count("#main img") > 1
        assert app.errors() == []


def test_a_browse_tile_is_captioned_with_the_file_it_shows(open_app):
    """The name, not the date: Browse already breaks the grid into dated
    sections, so a date under every tile repeats the heading above it, while
    the name is the one thing on screen that says which file this is."""
    with open_app("library", wait_for=".tile") as app:
        caption = app.tab.evaluate("document.querySelector('#grid .tile .cap-label').textContent")
        title = app.tab.evaluate("document.querySelector('#grid .tile .cap-label').title")

        assert caption.endswith(".jpg")
        # Truncation is CSS, so the whole name has to survive somewhere a
        # reader can get at it.
        assert title == caption
        assert app.errors() == []


# Holds /api/browse/filters -- and only it -- until the test lets it go, which
# is what a cold page cache does to that request on a real archive: it is a
# pass over every file, so it can take seconds while every other request is
# quick. Patching fetch rather than the network layer keeps the delay to the
# one URL, so nothing else about the screen is slowed down or changed.
HOLD_FILTERS_JS = """
(() => {
  const realFetch = window.fetch.bind(window);
  let held = null;
  window.__holdFilters = () => { held = new Promise(r => { window.__releaseFilters = r; }); };
  window.fetch = async (url, opts) => {
    if (held && String(url).includes('/api/browse/filters')) await held;
    return realFetch(url, opts);
  };
  window.__holdFilters();
})()
"""


def test_browse_is_usable_while_its_filter_options_are_still_loading(open_app):
    """The screen must not wait on the filter options for anything but the
    filter bar.

    They are the slowest thing Browse asks for and the only one it cannot draw
    without, and awaiting them up front meant the whole screen sat empty for
    the sum of both requests: no sort control, no media, and an unstyled gap
    where the filters would go. So: the sort control is populated, the grid has
    fetched and painted, and the filter bar stands in for itself at its settled
    size -- all before the options have arrived at all.
    """
    with open_app("overview") as app:
        app.tab.evaluate(HOLD_FILTERS_JS)
        app.tab.evaluate("showSection('library')")

        # The grid got its own request away rather than queueing behind the
        # filters, which is the whole point.
        app.wait_for("#grid .tile")
        assert app.count("#f-sort option") > 0, "the sort control waited for the filter options"
        assert app.count("#filterbar .fsel-loading") > 0, "the filter bar left an empty gap"
        assert app.count("#filterbar select") == 0, "the real controls cannot exist yet"

        app.tab.evaluate("window.__releaseFilters()")

        app.wait_for("#f-clear")
        assert app.count("#filterbar .fsel-loading") == 0, "the placeholders outlived the options"
        assert app.errors() == []


def test_returning_to_browse_keeps_the_filter_options_it_already_had(open_app):
    """Which years, people and places an archive has does not change while the
    user steps over to another screen and back, and re-deriving it costs a pass
    over every file. So the second visit draws its bar from what the first one
    fetched -- proven here by holding the request that would rebuild it and
    finding the real controls on screen regardless.
    """
    with open_app("library") as app:
        app.wait_for("#f-clear")
        app.show_section("overview")
        app.tab.evaluate(HOLD_FILTERS_JS)

        app.tab.evaluate("showSection('library')")

        app.wait_for("#f-place")
        assert app.count("#filterbar .fsel-loading") == 0, "the bar was rebuilt from nothing"
        app.tab.evaluate("window.__releaseFilters()")
        assert app.errors() == []


@needs_face_detection
def test_the_people_screen_lists_the_named_people(open_app):
    with open_app("people") as app:
        app.wait_for_text("Ada")
        assert "Grace" in app.text("#main")
        assert app.errors() == []


def test_the_pets_screen_lists_the_named_pets(open_app):
    """Both names, but only the first is waited for.

    The two cards come from one response, so once either has painted the other
    has too -- and asserting the second directly is what would catch a listing
    that renders only its first row.
    """
    with open_app("pets") as app:
        app.wait_for_text("Kira")
        assert "Rex" in app.text("#main")
        assert app.errors() == []


def test_the_places_screen_draws_a_map_without_reaching_a_tile_server(open_app):
    """Leaflet's own container is the check: the map is the one screen whose
    content is a third-party widget rather than the app's own markup, so
    "there is text in #main" would pass with no map at all.

    Tile hosts are blocked at the protocol level for this tier (see
    conftest.TILE_HOSTS), which also keeps the run honest about the project's
    no-network rule -- the basemap is empty here on purpose.
    """
    with open_app("places") as app:
        app.wait_for(".leaflet-container")
        assert app.errors() == []


def test_browse_says_what_it_can_search_before_anything_is_typed(open_app):
    """The panel that replaced a one-line blurb. Every way this archive can
    answer a query gets a row saying what it matches, so the screen states what
    it can do rather than waiting to be asked."""
    from trove import features

    with open_app("library", wait_for=".way") as app:
        shown = app.tab.evaluate(
            "[...document.querySelectorAll('.way-text b')].map(e => e.textContent)"
        )
        # Against the catalogue rather than against strings typed here: Browse is
        # the fourth screen to name this work, and the whole point of composing
        # these server-side is that it cannot call it something else.
        expected = [w.label for w in features.search_ways(features.ids())]
        assert [w.removesuffix("always") for w in shown] == expected
        assert shown[0].endswith("always"), "file names is not a feature anyone chose"
        assert app.errors() == []


def test_a_way_links_to_the_page_that_documents_it(open_app):
    """Every feature feeding a way gets its own way in. The text way has two,
    which is why they are marks rather than a row of link text."""
    with open_app("library", wait_for=".way") as app:
        links = app.tab.evaluate(
            "[...document.querySelectorAll('.way .way-doc')].map(e => e.title)"
        )
        assert "How Search by document text works" in links
        assert "How Search by picture text works" in links
        assert "How Search by description works" in links
        assert app.errors() == []


def test_a_way_says_what_it_holds_rather_than_how_much_was_done_to_it(open_app):
    """The second half of a way's row is how much of the archive it can see.

    It used to be a total and the name of the thing the stage did to it -- "12
    read · 40 passages", "8,900 photos and videos indexed" -- where the total
    answers nothing on its own and the verb is the pipeline's vocabulary. What
    decides whether a way can answer your question is *what kind of thing* it
    holds, so that is what the line counts.
    """
    with open_app("library", wait_for=".way") as app:
        cover = app.tab.evaluate(
            "Object.fromEntries([...document.querySelectorAll('.way')].map(e => ["
            " e.querySelector('.way-text b').textContent.replace('always', ''),"
            " e.querySelector('.way-cov').textContent]))"
        )

        # The fixture reads two documents and one picture, and has indexed
        # neither of them by description yet.
        text = cover["Search by text extracted"]
        assert "2 documents" in text and "1 image" in text
        assert "passages" not in text and "read" not in text
        # Every file is searchable by name, so there is no share of them to
        # qualify -- the count is the whole archive, with nothing after it.
        assert re.fullmatch(r"[\d,]+ files", cover["Search by filename"])
        # A backlog is still worth saying: it is the part that will change.
        assert "queued" in text
        assert app.errors() == []


def test_the_timeline_offers_a_person_named_since_it_was_last_open(open_app):
    """The Timeline's people filter is built once and then set aside with the
    screen, so naming someone in People never reached it.

    Browse looks right for a reason that does not apply here: its DOM is
    released when you leave rather than stashed, so its bar is rebuilt from
    nothing every visit. The Timeline keeps its chart and its scroll, and kept
    its stale list of names along with them.
    """
    with open_app("timeline") as app:
        app.wait_for("#tl-people-filter")
        before = app.tab.evaluate(
            "[...document.querySelectorAll('#tl-people-filter .multi-option')]"
            ".map(e => e.textContent.trim())"
        )
        assert "Newly Named" not in before

        app.show_section("people")
        app.wait_for(".pcard .pname")
        app.tab.evaluate("document.querySelector('.pcard .pname').click()")
        app.wait_for(".pcard .pmeta-editing input")
        app.tab.evaluate(
            "(() => { const i = document.querySelector('.pcard .pmeta-editing input');"
            " i.value = 'Newly Named'; i.dispatchEvent(new Event('blur')); })()"
        )
        app.wait_for_text("Newly Named")

        app.tab.evaluate("showSection('timeline')")
        app.tab.wait_for(
            "[...document.querySelectorAll('#tl-people-filter .multi-option')]"
            ".some(e => e.textContent.includes('Newly Named'))",
            timeout=10.0,
            what="the newly named person to reach the Timeline's filter",
        )
        assert app.errors() == []
