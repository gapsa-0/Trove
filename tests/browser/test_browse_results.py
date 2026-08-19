"""What a search comes back as: the groups, and how much of each one is shown.

Split from test_screens.py at the seam the frontend itself is split at
(static/js/results.js, static/css/results.css): that module asks whether each
screen renders at all, this one asks what a *query* produces -- the group per
ranking, the two rows of it the overview shows, the one opened in full, the
line naming the ways that came back empty, and the scope control that says how
far down a description ranking to go.
"""

from __future__ import annotations

import urllib.request

from seed import BROWSABLE_MEDIA


def _search(app, text):
    """Type into the composer and submit it, the way a person does."""
    app.tab.evaluate(
        "(() => { const c = document.getElementById('semantic-q');"
        f" c.textContent = {text!r};"
        " c.closest('form').dispatchEvent("
        "new Event('submit', {cancelable: true, bubbles: true})); })()"
    )


def _columns(app, grid_id):
    """How many columns that grid is currently laid out in."""
    return app.tab.evaluate(
        f"getComputedStyle(document.getElementById({grid_id!r}))"
        ".gridTemplateColumns.split(' ').filter(t => t.endsWith('px')).length"
    )


def test_a_search_shows_two_rows_of_each_ranking_and_offers_the_rest(open_app):
    """The overview: every ranking previewed, none of them paging.

    Three groups stacked in one scroller, each extending itself as its own foot
    scrolls into view, means the second group sits behind the whole of the first
    and the third is unreachable -- which is what this replaces. So a preview is
    capped at whole rows and the group's sentinel promises nothing, because a
    group that is not going to page must not say "scroll to load more".

    Rows rather than a fixed count: a number of results lands mid-row at every
    width but one, and a half-filled last row is how a grid says "that is all of
    them", which is the opposite of what a preview means.
    """
    with open_app("library", wait_for=".tile") as app:
        app.wait_for("#f-clear")
        _search(app, "photo")
        app.wait_for("#grid-name .tile")
        app.wait_for("#more-name .more-btn")

        shown = app.count("#grid-name .tile")
        assert shown == _columns(app, "grid-name") * 2, "a preview is whole rows"
        assert f"Show all {BROWSABLE_MEDIA}" in app.text("#more-name"), (
            "the button names the whole ranking"
        )
        assert app.text("#grid-name-sentinel") == "", "a previewed group promises no paging"

        # The cap holds against the scroll that used to extend it. The ranking
        # runs to more than one page (120), so there is genuinely more to fetch.
        app.scroll_to_bottom()
        app.tab.evaluate("new Promise(r => setTimeout(r, 400))")
        assert app.count("#grid-name .tile") == shown
        assert app.errors() == []


def test_opening_a_ranking_gives_the_whole_screen_to_it(open_app):
    """ "Show all" is the way past two rows, and what it opens is one ranking on
    its own: the others are not on screen, the line naming the ways that found
    nothing is not either -- it is a note about ways you have set aside -- and
    the count beside the sort control is this ranking's rather than the sum of
    three.

    And this is the group that pages now, which is the whole point: the results
    past the first page are reachable by scrolling, without a second ranking
    underneath being pushed further away by every one of them.
    """
    with open_app("library", wait_for=".tile") as app:
        app.wait_for("#f-clear")
        _search(app, "photo")
        app.wait_for("#more-name .more-btn")

        app.click("#more-name .more-btn")

        assert app.count(".results-group:not([hidden])") == 1
        assert app.count("#group-name[hidden]") == 0
        assert app.count("#nothing-line[hidden]") == 1
        assert app.count("#results-back:not([hidden]) .back-btn") == 1
        assert app.text("#gridcount") == f"{BROWSABLE_MEDIA} results"
        assert app.count("#more-name .more-btn") == 0, "nothing left to open"
        # One page of it, and the rest reachable by scrolling.
        assert app.count("#grid-name .tile") == 120
        app.scroll_to_bottom()
        app.tab.wait_for(
            "document.querySelectorAll('#grid-name .tile').length > 120",
            what="the opened ranking to page past its first 120",
        )
        assert app.errors() == []


def test_the_way_back_returns_to_the_preview_of_every_ranking(open_app):
    """Back is a repaint, not a reload: every group still holds the pages it
    fetched, so what comes back is the same overview at the same totals."""
    with open_app("library", wait_for=".tile") as app:
        app.wait_for("#f-clear")
        _search(app, "photo")
        app.wait_for("#more-name .more-btn")
        preview = app.count("#grid-name .tile")

        app.click("#more-name .more-btn")
        app.wait_for("#results-back:not([hidden])")
        app.click(".back-btn")

        assert app.count("#results-back[hidden]") == 1
        assert app.count("#grid-name .tile") == preview
        assert f"Show all {BROWSABLE_MEDIA}" in app.text("#more-name")
        assert app.text("#gridcount") == f"{BROWSABLE_MEDIA} results", (
            "the total is every ranking's again"
        )
        assert app.errors() == []


def test_narrowing_a_filter_leaves_you_reading_the_same_ranking(open_app):
    """Which ranking you are reading is not a filter on the library, so it does
    not clear with one. Narrowing what you are looking at is no reason to stop
    looking at it -- and the filters, the sort and the result scope all reload
    the grids through the same path, so this is the claim that keeps the reset
    on the search box where it belongs."""
    with open_app("library", wait_for=".tile") as app:
        app.wait_for("#f-clear")
        _search(app, "photo")
        app.wait_for("#more-name .more-btn")
        app.click("#more-name .more-btn")
        app.wait_for("#results-back:not([hidden])")

        app.tab.evaluate(
            "(() => { const s = document.getElementById('f-type');"
            " s.value = 'image'; applyFilters(); })()"
        )
        app.wait_for("#grid-name .tile")

        assert app.count("#results-back:not([hidden])") == 1, "still inside the ranking"
        assert app.count(".results-group:not([hidden])") == 1
        assert app.errors() == []


def test_a_new_search_leaves_the_ranking_the_last_one_was_read_in(open_app):
    """Which way answers you best is a property of what you typed, so a new
    query gets the summary of every way and lets you choose again."""
    with open_app("library", wait_for=".tile") as app:
        app.wait_for("#f-clear")
        _search(app, "photo")
        app.wait_for("#more-name .more-btn")
        app.click("#more-name .more-btn")
        app.wait_for("#results-back:not([hidden])")

        # A narrower query, so the overview it lands on is demonstrably the new
        # search's rather than what was on screen a moment ago.
        _search(app, "photo0")
        # photo000 to photo099, less the one of them that is a duplicate copy
        # and so hidden from browsing.
        narrower = 99
        app.tab.wait_for(
            f"document.querySelector('#gridcount').textContent === '{narrower} results'",
            what="the second search to answer",
        )

        assert app.count("#results-back[hidden]") == 1
        assert f"Show all {narrower}" in app.text("#more-name")
        assert app.errors() == []


def test_the_viewer_walks_what_the_preview_shows_and_no_more(open_app):
    """The arrows step through `S.gallery`, and a preview is the first case
    where the results a group holds and the results it is showing differ.

    Built from the loaded pages, a preview of ten would have handed the viewer
    all hundred and thirty: arrowing right off the last tile on screen would
    have walked into results with nothing on screen to show for them. So the
    gallery is read back off the tiles, and it says which set it is bounded by.
    """

    def open_first_tile():
        # Emptied first: the readout survives a closed viewer, so waiting for it
        # to have content is otherwise answered instantly by the last one's.
        app.tab.evaluate(
            "(() => { document.getElementById('vpos').textContent = '';"
            " document.querySelector('#grid-name .tile').click(); })()"
        )
        return app.tab.wait_for(
            "(document.getElementById('vpos').textContent || null)",
            what="the viewer's position readout",
        )

    with open_app("library", wait_for=".tile") as app:
        app.wait_for("#f-clear")
        _search(app, "photo")
        app.wait_for("#more-name .more-btn")
        shown = app.count("#grid-name .tile")

        assert open_first_tile() == f"1of{shown}· in these results"

        app.tab.evaluate("closeModal()")
        app.click("#more-name .more-btn")
        app.wait_for("#results-back:not([hidden])")

        assert open_first_tile() == "1of120· in filename matches", (
            "inside one ranking the arrows are bounded by that ranking, and say so"
        )
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


def test_a_way_that_found_nothing_is_named_by_what_you_typed_against(open_app):
    """The collapsed line is the answer for every ranking that came back
    empty -- "the documents were searched and none matched" is worth knowing,
    and its absence used to leave people wondering whether a feature had run.

    It names the ways the way the sentence already reads. Printing their whole
    labels gave "Nothing found in Search by filename or Search by description",
    which is the same two words three times in one line; the ways are all
    called "Search by <something>", so the line says it once.

    And it sits above the groups it qualifies: under a screenful of results it
    is a footnote nobody scrolls to, and the reader who needs it most is the
    one who stops at the top.
    """
    with open_app("library", wait_for=".tile") as app:
        app.wait_for("#f-clear")
        app.tab.evaluate(
            "document.querySelector('#semantic-q').textContent = 'zzqqxx';"
            "document.querySelector('.library-search').requestSubmit()"
        )
        app.wait_for("#nothing-line:not([hidden])")

        line = app.text("#nothing-line")
        assert "found by filename" in line
        assert "Search by" not in line
        # One mark per way named, since the marks are how results are labelled
        # everywhere else on this screen.
        assert app.count("#nothing-line .ranking-mark") == app.count("#nothing-line .nl-item")
        # The sibling combinator only matches groups that follow the line, so
        # this is the ordering claim and not just "both are on the page".
        assert app.count("#nothing-line ~ .results-group") == app.count(".results-group")
        assert app.errors() == []


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


def test_widening_to_all_results_leaves_you_where_you_were_reading(open_app):
    """ "All results" adds results below the ones already on screen.

    It is the same ranking in the same order with the relevance floor taken off
    (routes/search.py), so everything you were looking at is still there and
    still in that place. Being thrown to the top for it was the screen
    answering "show me more" by taking away what you had.

    The scroller is given something to scroll that is not the results
    themselves: this tier has no embedding model, so a description search comes
    back empty and there would be no height to hold a position in. The padding
    sits outside every group, which is exactly what makes it survive the reload
    under test. Half of the travel rather than a round number, and read back
    rather than assumed, so neither this screen's height nor the few pixels the
    reload takes off it can decide whether the test passes.
    """
    with open_app("library") as app:
        app.wait_for("#f-clear")
        _search(app, "the beach")
        app.wait_for(".aq-scope")
        # The search's own pages are still landing, and each one re-anchors the
        # scroll as it renders. Scrolling into that races it.
        app.wait_until_settled()
        before = app.tab.evaluate(
            "(() => { const m = document.getElementById('main');"
            " const pad = document.createElement('div');"
            " pad.style.height = '4000px'; m.appendChild(pad);"
            " m.scrollTop = Math.floor((m.scrollHeight - m.clientHeight) / 2);"
            " return m.scrollTop; })()"
        )
        assert before > 0, "the results screen did not scroll, so this proves nothing"

        app.click(".aq-scope button:last-child")

        assert app.tab.evaluate("document.getElementById('main').scrollTop") == before, (
            "widening the cut scrolled back to the top"
        )
        # ...and still there once the reload has landed and had its say.
        app.tab.wait_for(
            "document.querySelector('.aq-scope button[aria-pressed=\"true\"]')"
            ".textContent === 'All results'",
            timeout=5.0,
            what="the scope control to settle on All results",
        )
        assert app.tab.evaluate("document.getElementById('main').scrollTop") == before
        assert app.errors() == []
