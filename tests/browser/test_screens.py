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


def test_library_health_draws_the_pipeline_as_a_chain(open_app):
    """The Overview's rail. It is DOM and CSS with no other coverage, and the
    thing it says — Indexing and Duplicates first, everything else hanging off
    them — is the same thing the setup screen says before any of it runs.

    Asserted through the trunk marking rather than by counting rows, because
    which optional stages an archive runs is its own business; that the first
    two are the ones nobody can decline is not.
    """
    with open_app("overview") as app:
        # A node, not a row: the "Checking for work…" placeholder is a row too,
        # and has no rail, no head and no label to read.
        app.wait_for(".health-node")
        labels = app.tab.evaluate(
            "[...document.querySelectorAll('.health-task')]"
            ".map(e => [e.classList.contains('trunk'),"
            " e.querySelector('.health-task-head').textContent.trim()])"
        )
        assert [label for trunk, label in labels if trunk] == ["Indexing", "Duplicates"]
        assert labels[0][1] == "Indexing" and labels[1][1] == "Duplicates"
        # One node per row, and a rail to hang them on.
        assert app.count(".health-task .health-node") == len(labels)
        assert app.errors() == []


def test_a_health_row_without_a_rail_still_gets_the_full_width(open_app):
    """The rail is two grid tracks, and a row that has no rail — the
    "Checking for work…" placeholder, since there is no chain to draw until
    the first poll says what it is — flowed into the 20px track and wrapped
    one letter per line. Asserted on the layout rule rather than on the
    placeholder's markup, because the rule is what has to hold for any row.
    """
    with open_app("overview") as app:
        app.wait_for(".health-node")
        width = app.tab.evaluate("""
          (() => {
            const grid = document.querySelector('.health-grid');
            grid.insertAdjacentHTML('beforeend',
              '<div class="health-task" id="railless">'
              + '<div class="health-task-body">x</div></div>');
            const w = document.querySelector('#railless .health-task-body').offsetWidth;
            document.getElementById('railless').remove();
            return w;
          })()
        """)
        assert width > 200, f"a railless row collapsed to {width}px"
        assert app.errors() == []


def test_the_library_grid_fills_with_the_archives_media(open_app):
    with open_app("library") as app:
        app.wait_for("#main img")
        assert app.count("#main img") > 1
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


def test_the_result_scope_appears_with_the_search_it_belongs_to(open_app):
    """It lives on the query's own line, so it exists only while one is running.

    Not in the filter bar and not a checkbox: this says how much of one
    search's ranking is on screen, which is a different kind of question from
    "which files count", and with nothing searched there is no ranking to
    widen. Driven through the form's own submit handler rather than by calling
    the renderer, so what is checked is the path a user takes.

    The search itself needs the embedding model and will fail in this tier --
    which is fine and is the point: the control belongs to the *search*, so it
    has to appear when the query is submitted rather than when results come
    back, or it would never show up on the searches that returned nothing.
    """
    with open_app("library") as app:
        # The filter bar is built after the screen's markup lands, so waiting
        # on #main alone would let the submit below race renderPhotos.
        app.wait_for("#f-clear")
        assert app.count(".aq-scope") == 0

        app.tab.evaluate(
            "document.querySelector('#semantic-q').textContent = 'the beach';"
            "document.querySelector('.library-search').requestSubmit()"
        )

        app.wait_for(".aq-scope")
        assert (
            app.tab.evaluate(
                "document.querySelector('.aq-scope button[aria-pressed=\"true\"]').textContent"
            )
            == "Top matches"
        )


def test_choosing_all_results_widens_the_search_it_is_attached_to(open_app):
    """Clicking the other segment moves the state the request is built from.

    Asserted through which segment is lit rather than on results: this tier has
    no embedding model, so the fetch behind the click cannot succeed. That is
    not a weaker check than reading the state directly -- the pressed segment
    is rendered *from* `topMatchesOnly`, so it can only move if the grid state
    moved with it, and that state is what decides whether `top=no` is sent.
    """
    with open_app("library") as app:
        app.wait_for("#f-clear")
        app.tab.evaluate(
            "document.querySelector('#semantic-q').textContent = 'the beach';"
            "document.querySelector('.library-search').requestSubmit()"
        )
        app.wait_for(".aq-scope")

        app.click(".aq-scope button:last-child")

        assert (
            app.tab.evaluate(
                "document.querySelector('.aq-scope button[aria-pressed=\"true\"]').textContent"
            )
            == "All results"
        )
        assert app.count(".aq-scope button[aria-pressed='true']") == 1
