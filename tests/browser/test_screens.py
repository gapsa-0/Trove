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
import urllib.request

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


def test_opening_a_copy_bounds_the_arrows_to_its_own_group(open_app):
    """The viewer walks S.gallery, so what a screen puts there is a claim about
    what "next" means. Filling it with every copy on the page ran the arrows off
    the end of the group being compared and into the next group's photographs,
    which are a different picture entirely.
    """
    with open_app("dups", wait_for=".duptile") as app:
        # A second group on the page, so "this group" and "everything shown"
        # are different sets -- the fixture alone holds one group, where the
        # old behaviour and the new one are indistinguishable.
        tiles = app.tab.evaluate("""
          (() => {
            const group = document.querySelector('.dupgroup');
            const extra = group.cloneNode(true);
            extra.querySelectorAll('[data-file-id]').forEach((el, i) => {
              el.dataset.fileId = String(9000 + i);
            });
            group.after(extra);
            return document.querySelectorAll('.duptile[data-file-id]').length;
          })()
        """)
        assert tiles == 4, f"expected two groups of two, got {tiles} tiles"

        app.tab.evaluate("document.querySelector('.duptile').click()")
        app.wait_for(".vpos b")
        readout = app.text(".vpos")

        # Two, not four: the arrows stop at the end of the group.
        assert "of" in readout and "2" in readout, readout
        assert "in this duplicate group" in readout, readout
        assert app.errors() == []


def test_the_listing_can_be_filtered_and_sorted(open_app):
    """Both controls send the listing back to the server rather than hiding
    rows: the list is paged, so most matching groups are still there."""
    with open_app("dups", wait_for=".duptile") as app:
        requested = app.tab.evaluate("""
          (() => {
            const seen = [];
            const original = window.fetch;
            window.fetch = (...args) => { seen.push(String(args[0])); return original(...args); };
            document.getElementById('dup-match').value = 'visual';
            document.getElementById('dup-match').dispatchEvent(new Event('change'));
            document.getElementById('dup-sort').value = 'count_asc';
            document.getElementById('dup-sort').dispatchEvent(new Event('change'));
            window.fetch = original;
            return JSON.stringify(seen.filter(u => u.includes('/api/dups')));
          })()
        """)
        import json

        urls = json.loads(requested)
        assert any("match=visual" in u for u in urls), urls
        assert any("sort=count_asc" in u for u in urls), urls
        app.wait_for("#dup-count")
        assert app.errors() == []


def test_a_group_wraps_onto_a_second_line_instead_of_scrolling_sideways(open_app):
    """A group is a set of copies to compare against each other, and a copy
    parked off the right edge cannot be compared with anything. Asserted as
    layout rather than markup: the tiles are injected here so the rule is
    checked against a group big enough to overflow, which the fixture's pair
    never is.
    """
    with open_app("dups", wait_for=".duptile") as app:
        rows = app.tab.evaluate("""
          (() => {
            const row = document.querySelector('.duprow');
            const tile = row.querySelector('.duptile');
            for (let i = 0; i < 30; i++) row.appendChild(tile.cloneNode(true));
            const tops = new Set([...row.querySelectorAll('.duptile')]
              .map(t => t.getBoundingClientRect().top));
            return [tops.size, row.scrollWidth <= row.clientWidth];
          })()
        """)
        lines, fits = rows
        assert lines > 1, "30 copies stayed on one line"
        assert fits, "the group still overflows its own width"
        assert app.errors() == []


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


def test_browse_shows_no_group_headings_until_there_is_a_search(open_app):
    """Browsing is the plain dated listing, not a result: a label telling you
    which of one thing you are looking at is noise. The rankings that answer a
    query stay out of the way until one is asked."""
    with open_app("library", wait_for=".tile") as app:
        assert app.count("#group-text[hidden]") == 1
        assert app.count("#group-name[hidden]") == 1
        assert app.count("#nothing-line[hidden]") == 1
        # The media group is the listing, so it is on screen without a heading.
        assert app.count("#group-media[hidden]") == 0
        assert app.count("#group-media.plain") == 1
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


def test_a_search_finds_a_word_inside_a_document(open_app):
    """The second group, driven the way a person drives it: type into the box,
    submit the form, read what comes back.

    The word searched for is in no filename -- only inside the documents -- so a
    hit proves the text was read, indexed, matched and rendered end to end. The
    group goes above the media one because an exact word match is explainable in
    a way a cosine is not.
    """
    with open_app("library", wait_for=".tile") as app:
        app.tab.evaluate(
            "(() => { const c = document.getElementById('semantic-q');"
            " c.textContent = 'lease';"
            " c.closest('form').dispatchEvent("
            "new Event('submit', {cancelable: true, bubbles: true})); })()"
        )
        app.wait_for("#grid-text .tile")

        assert app.count("#group-text[hidden]") == 0
        assert app.count("#grid-text .tile") == 2
        body = app.text("#grid-text")
        assert "lease" in body.lower()
        assert "p. 2" in body, "a hit says which page its passage came from"
        assert app.count("#grid-text mark") > 0, "the matched word is marked"
        assert app.errors() == []


def test_an_extension_typed_beside_a_word_stays_a_word_of_its_own(open_app):
    """Searching filenames is the one way every archive has, and `.pdf` on the
    end of a query is how you say "and it is a PDF".

    The composer tidies the sentence left behind when a person's name is lifted
    out of it, and that tidy-up used to close the gap in front of ANY
    punctuation -- so ` .jpg` became `.jpg` glued to the word before it, and
    `2 .jpg` searched the names ENDING in `2.jpg` rather than the ones holding
    both words. Asserted by typing the same two words in both orders: a filter
    over ANDed words cannot care which came first.
    """

    def search(text):
        # The previous search's count is cleared first, so waiting for one is
        # not answered instantly by the number already on screen.
        app.tab.evaluate(
            "(() => { const c = document.getElementById('semantic-q');"
            f" c.textContent = {text!r};"
            " document.getElementById('gridcount-name').textContent = '';"
            " c.closest('form').dispatchEvent("
            "new Event('submit', {cancelable: true, bubbles: true})); })()"
        )
        return app.tab.wait_for(
            "document.getElementById('gridcount-name').textContent",
            what=f"the filename group to count what {text!r} found",
        )

    with open_app("library", wait_for=".tile") as app:
        trailing = search("2 .jpg")
        leading = search(".jpg 2")

        assert trailing == leading, "the extension was glued to the word before it"
        assert app.errors() == []


def test_a_photograph_and_a_document_are_the_same_size_in_the_text_group(open_app, archive):
    """Both text features write into the same passages, so this group holds both
    kinds of file -- and a result should look like a result whichever
    it is.

    It did not. A thumbnail is `height: 100%`, which the square tile bounds
    everywhere else and this column bounded nowhere: the photograph grew to fill
    it, the grid row stretched to match, and a document holding one line of text
    got a cell four times the height of its own content. The assertion is on the
    picture each result shows of itself, since that is the part that varied.
    """
    # Generate the thumbnail before the page asks for it. A tile's `<img>`
    # removes itself on error, so a slow first generation does not leave this
    # waiting -- it leaves the element gone and the bug unreproducible, which
    # would pass silently rather than fail.
    #
    # By the route that names its archive, not `/thumb/<id>`: that one resolves
    # against whichever archive is *open*, and nothing is open until the app
    # below starts. Both write the same cache entry, so the page's own request
    # finds it already there.
    urllib.request.urlopen(
        f"{archive.base_url}/archivethumb/{archive.root_id}/{archive.ids['ocr_photo']}"
    ).read()

    with open_app("library", wait_for=".tile") as app:
        app.tab.evaluate(
            "(() => { const c = document.getElementById('semantic-q');"
            " c.textContent = 'receipt';"
            " c.closest('form').dispatchEvent("
            "new Event('submit', {cancelable: true, bubbles: true})); })()"
        )
        app.wait_for("#grid-text .tile")
        app.wait_for("#grid-text img")

        media = app.tab.evaluate(
            "[...document.querySelectorAll('#grid-text .tile')].map(t => Math.round("
            "(t.querySelector(':scope > img') || t.querySelector(':scope > .ph'))"
            ".getBoundingClientRect().height))"
        )
        assert len(media) == 2, "this search has to return a photograph and a document"
        assert len(set(media)) == 1, f"the media boxes disagree: {media}"
        assert max(media) < 140, f"a thumbnail grew to fill the column: {media}"
        # A photograph's own thumbnail is the most recognisable thing about it,
        # so the picture is what it shows -- not the generic glyph. Asked of the
        # photograph rather than as a count over the group: a document can carry
        # a thumbnail too now (a PDF renders its first page), so "exactly one
        # image here" would be pinning the absence of that rather than this.
        assert app.count(f"#grid-text .tile[data-file-id='{archive.ids['ocr_photo']}'] img") == 1
        assert app.errors() == []


def test_a_photograph_found_by_its_writing_says_so(open_app):
    """The badge that tells a scanned receipt from a PDF's own text layer, drawn
    only where both readers are on and a hit could be either."""
    with open_app("library", wait_for=".tile") as app:
        app.tab.evaluate(
            "(() => { const c = document.getElementById('semantic-q');"
            " c.textContent = 'recibo';"
            " c.closest('form').dispatchEvent("
            "new Event('submit', {cancelable: true, bubbles: true})); })()"
        )
        app.wait_for("#grid-text .found-by")
        assert "text in pictures" in app.text("#grid-text .found-by")
        assert app.errors() == []
