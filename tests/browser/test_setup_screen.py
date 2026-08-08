"""The archive setup screen, driven in a real browser.

Five things here have no other coverage and each fails silently:

* the screen renders at all -- it is built from a template literal, so a typo
  produces an empty panel rather than an error;
* dragging a card onto the pipeline does the same thing as pressing Add, and
  dragging a link back out does the same thing as pressing its remove button,
  which is the accessibility promise the screen is built on;
* resting the pointer on a card turns it over to what the feature does, without
  moving the grid and without the description being cut off;
* the name field belongs to this visit: what was typed survives the re-render
  adding a feature performs, and does not survive the panel being reopened for
  a different folder;
* the nav really loses the sections an archive's features do not unlock, rather
  than offering a screen whose data will never arrive.
"""

from __future__ import annotations

import json
import urllib.request


def _configure(archive, **body):
    """Change the archive's setup through the API the screen itself posts to."""
    request = urllib.request.Request(
        f"{archive.base_url}/api/archive/configure",
        data=json.dumps({"root_id": archive.root_id, **body}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read())


def _open_setup(app, path="/tmp/example-folder"):
    """Open setup the way the picker does: for a folder that is not an archive
    yet, which -- since the Features sheet took over changing what a live one
    runs -- is the only way this screen opens at all.

    Waits on the path the panel was opened *for*, not on the panel: closing
    setup only hides it, so its last visit's markup is still in the DOM and
    anything that merely waits for a chip sails straight through the render.
    """
    app.tab.evaluate(f"import('/static/js/setup.js').then(m => m.openArchiveSetup({path!r}))")
    app.tab.wait_for(
        f"(document.querySelector('#setup .set-path') || {{}}).textContent === {path!r}",
        what=f"the setup panel to open on {path!r}",
    )


def _type_name(app, text):
    """Type into the name field, event and all -- that event is the mechanism."""
    app.tab.evaluate(
        "(t => { const f = document.getElementById('setup-name'); f.value = t;"
        f" f.dispatchEvent(new Event('input', {{ bubbles: true }})); }})({text!r})"
    )


def _name(app):
    return app.tab.evaluate("document.getElementById('setup-name').value")


# Placeholders rather than an f-string: the body is JavaScript, and every brace
# in it would otherwise have to be doubled.
_DRAG_JS = """(() => {
  const held = document.querySelector(SOURCE);
  const target = document.querySelector(TARGET);
  const dt = new DataTransfer();
  held.dispatchEvent(new DragEvent('dragstart', { dataTransfer: dt, bubbles: true }));
  const lit = document.querySelectorAll('#setup .drop-open').length;
  target.dispatchEvent(new DragEvent('drop',
    { dataTransfer: dt, bubbles: true, cancelable: true }));
  return lit;
})()"""


def _drag(app, source, target):
    """Drag one element onto another through real DragEvents.

    Returns how many drop targets lit up, which is the assertion that the
    screen offers only the one that would change something.
    """
    return app.tab.evaluate(
        _DRAG_JS.replace("SOURCE", repr(source)).replace("TARGET", repr(target))
    )


def _choose_folder(app, path):
    """Type a folder into the start page's field and submit it, the way the
    form is used when there is no desktop folder chooser."""
    app.tab.evaluate(
        f"(field => {{ field.value = {path!r};"
        " field.closest('form').requestSubmit(); })"
        "(document.getElementById('archive-path'))"
    )


def test_a_folder_that_is_already_an_archive_never_reaches_setup(open_app, archive):
    """The refusal used to arrive at the end: choose a folder, configure eight
    features, press Create, and only then hear that the folder was already an
    archive. Nothing about that answer depends on what was configured -- it is
    true the moment the folder is chosen -- so it is asked there.

    And it names the archive rather than the fact, since at that point what you
    want to know is which of the folders on this page you just picked again.
    """
    with open_app() as app:
        app.wait_for(".p-card[data-archive]")

        _choose_folder(app, archive.ids["archive_path"])
        app.wait_for("#toast.show")

        assert "already an archive" in app.text("#toast")
        assert app.count(f'.p-card[data-archive="{archive.root_id}"].found') == 1
        assert (
            app.tab.evaluate("getComputedStyle(document.getElementById('setup')).display") == "none"
        ), "setup opened for a folder that could never be created"
        assert app.errors() == []


def test_a_folder_that_is_not_a_folder_never_reaches_setup(open_app, archive):
    """The other half of the same question, and the other thing add_archive
    used to refuse only at the end."""
    with open_app() as app:
        app.wait_for(".p-card[data-archive]")

        _choose_folder(app, archive.ids["archive_path"] + "/nothing-here")
        app.wait_for("#toast.show")

        assert "Not a directory" in app.text("#toast")
        assert (
            app.tab.evaluate("getComputedStyle(document.getElementById('setup')).display") == "none"
        )
        assert app.errors() == []


def test_the_pipeline_starts_with_only_the_features_that_cannot_be_removed(open_app):
    """Everything else waits on the shelf: pre-ticking them would pre-select
    about a gigabyte of downloads on the one screen meant to prevent that."""
    with open_app() as app:
        _open_setup(app)

        in_chain = app.tab.evaluate(
            "[...document.querySelectorAll('#set-flow .set-chip')].map(e => e.dataset.feature)"
        )
        assert in_chain == ["index", "duplicates"]
        assert app.count(".set-chip.fixed .set-chip-out") == 0, "neither may be removed"
        # Every feature keeps a card, chosen or not and declinable or not: the
        # card is the catalogue entry, and the chain is what was picked from it.
        assert app.count(".set-card") >= 3
        assert app.count(".set-card.fixed") == 2
        assert "0 MB" in app.text(".set-total")
        assert app.errors() == []


def test_the_stages_that_always_run_have_a_card_that_cannot_be_switched_off(open_app):
    """They used to be links in the chain and a two-row note under it, so the
    one screen whose job is deciding what runs described six of the eight
    things it was about to do -- leaving out the two the other six read from.

    A card each, with "Always runs" where the others carry Add, and nothing on
    it that offers to change that.
    """
    with open_app() as app:
        _open_setup(app)

        assert app.text('.set-card[data-feature="index"] .set-always') == "Always runs"
        assert app.count('.set-card[data-feature="index"] .set-add') == 0
        # The tagline is the point of giving them a card at all.
        assert "metadata" in app.text('.set-card[data-feature="index"] .set-card-line')

        # Pressing it is not a way to remove it, and neither is dragging it.
        app.click('.set-card[data-feature="duplicates"] .set-face')
        assert app.count('#set-flow .set-chip[data-feature="duplicates"]') == 1
        assert (
            app.tab.evaluate(
                "document.querySelector('.set-card[data-feature=\"duplicates\"]').draggable"
            )
            is False
        )
        # It turns over like any other card: what a stage does is worth reading
        # whether or not it is yours to decline.
        app.hover('.set-card[data-feature="duplicates"]')
        app.wait_shown('.set-card[data-feature="duplicates"] .set-back')
        assert app.errors() == []


_ADD_EVERYTHING = """(() => {
  let pill, guard = 20;
  while (guard-- && (pill = document.querySelector(
      '#set-shelf .set-card:not(.on):not(.off) .set-add'))) pill.click();
  const flow = document.getElementById('set-flow');
  const box = e => e.getBoundingClientRect();
  // Rows are counted off the steps, not the chips: the chip that just landed is
  // three pixels up for half a second (see .set-chip.landed), which reads as a
  // row of its own to anything measuring tops.
  const rows = new Set([...flow.querySelectorAll('.set-step')].map(e => Math.round(box(e).top)));
  const chips = [...flow.querySelectorAll('.set-chip')].map(box);
  // A link is 2px tall against a 28px chip, so "same row" is the chip's band
  // containing the link's middle -- not two equal tops.
  const orphans = [...flow.querySelectorAll('.set-link')].map(box).filter(link =>
    !chips.some(chip => chip.left < link.left
      && chip.top <= link.top + link.height / 2 && chip.bottom >= link.top));
  return [flow.scrollWidth <= flow.clientWidth, rows.size, orphans.length,
    flow.querySelectorAll('.set-turns path').length];
})()"""


def test_the_full_chain_wraps_instead_of_scrolling_sideways(open_app):
    """A pipeline with everything switched on is wider than the panel. It used
    to scroll, which put the far end of the chain behind an edge on the one
    summary of what the archive is about to do -- and asked someone to scroll a
    strip that is not the thing they operate.

    A chain that stops at one margin and starts again at the other is two
    chains, so every row change is drawn as one continuous turn -- and the
    connectors stay with their own chips, or a row would open with a line
    pointing at nothing.
    """
    with open_app() as app:
        _open_setup(app)

        fits, rows, orphans, turns = app.tab.evaluate(_ADD_EVERYTHING)

        assert fits, "the chain still overflows its own width"
        assert rows > 1, "the whole catalogue fitted on one row; nothing was proved"
        assert orphans == 0, f"{orphans} connectors opened a row"
        assert turns == rows - 1, f"{rows} rows joined by {turns} turns"
        assert app.errors() == []


# Where every turn begins, against where every row actually ends. Both are read
# off the live layout, so this holds at any width -- which is the point: the
# markup cannot say where a flexbox chose to wrap.
_TURNS_FIT_JS = """(() => {
  const flow = document.getElementById('set-flow');
  const box = flow.getBoundingClientRect();
  const steps = [...flow.querySelectorAll('.set-step')];
  const ends = [];
  for (let at = 0; at < steps.length - 1; at++) {
    const a = steps[at].getBoundingClientRect(), b = steps[at + 1].getBoundingClientRect();
    if (b.top >= a.bottom - 1) {
      ends.push([Math.round(a.right - box.left), Math.round(a.top - box.top + a.height / 2)]);
    }
  }
  const starts = [...flow.querySelectorAll('.set-turns path')].map(p => {
    const at = p.getAttribute('d').match(/^M([\\d.]+) ([\\d.]+)/);
    return [Math.round(+at[1]), Math.round(+at[2])];
  });
  return JSON.stringify({ends: ends, starts: starts});
})()"""


def test_the_chain_redraws_its_turn_when_the_panel_changes_width(open_app):
    """Where a flexbox wraps is not knowable from the markup, so the turn is
    measured -- which means it has to be measured again whenever the width
    changes, or it stays drawn across a row it no longer joins."""
    with open_app() as app:
        _open_setup(app)
        app.tab.evaluate(_ADD_EVERYTHING)
        before = app.tab.evaluate("document.querySelector('.set-turns path').getAttribute('d')")

        app.tab.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": 780, "height": 900, "deviceScaleFactor": 1, "mobile": False},
        )
        app.tab.wait_for(
            "document.querySelector('.set-turns path').getAttribute('d') !== " + repr(before),
            what="the chain to redraw its turn at the new width",
        )

        fit = json.loads(app.tab.evaluate(_TURNS_FIT_JS))
        assert fit["ends"], "no row wrapped at this width; nothing was proved"
        assert fit["starts"] == fit["ends"], "a turn no longer starts where its row ends"
        assert app.errors() == []


def test_dragging_a_card_onto_the_pipeline_adds_it(open_app):
    with open_app() as app:
        _open_setup(app)

        lit = _drag(app, '.set-card[data-feature="semantic"]', "#set-pipe")
        app.wait_for('#set-flow .set-chip[data-feature="semantic"]')

        assert lit == 1, "only the target that would change something is offered"
        assert "715 MB" in app.text(".set-total")
        # The card stays where it is and reports its new state.
        assert app.count('.set-card[data-feature="semantic"].on') == 1
        assert app.errors() == []


def test_dragging_a_link_back_to_the_shelf_removes_it(open_app):
    with open_app() as app:
        _open_setup(app)
        app.click('.set-card[data-feature="places"] .set-add')
        app.wait_for('#set-flow .set-chip[data-feature="places"]')

        _drag(app, '.set-chip[data-feature="places"]', "#set-shelf")

        assert app.count('#set-flow .set-chip[data-feature="places"]') == 0
        assert app.count('.set-card[data-feature="places"].on') == 0
        assert app.errors() == []


def test_the_button_does_the_same_job_as_the_drag(open_app):
    """Drag is an enhancement. A keyboard user reaches every feature through
    these buttons, so they cannot be a second-class path."""
    with open_app() as app:
        _open_setup(app)

        app.click('.set-card[data-feature="places"] .set-add')
        app.wait_for('#set-flow .set-chip[data-feature="places"]')
        app.click('.set-chip[data-feature="places"] .set-chip-out')

        assert app.count('#set-flow .set-chip[data-feature="places"]') == 0
        assert app.count('.set-card[data-feature="places"]') == 1
        assert app.errors() == []


def test_turning_a_card_over_does_not_move_the_others(open_app):
    """The two faces share one fixed-height card so that reading a description
    leaves the grid alone. An expanding panel moves everything beside it."""
    with open_app() as app:
        _open_setup(app)

        before = app.tab.evaluate(
            "[...document.querySelectorAll('.set-card')].map(e => Math.round("
            "e.getBoundingClientRect().height))"
        )
        app.hover('.set-card[data-feature="people"]')
        app.wait_shown('.set-card[data-feature="people"] .set-back')
        after = app.tab.evaluate(
            "[...document.querySelectorAll('.set-card')].map(e => Math.round("
            "e.getBoundingClientRect().height))"
        )

        assert before == after, "turning a card over resized the grid"
        assert len(set(after)) == 1, "the cards are not all the same height"
        # The description is the one from features.py, not the tagline again.
        assert "faces" in app.text('.set-card[data-feature="people"] .set-card-detail')
        assert app.errors() == []


def test_a_card_is_a_switch_on_whichever_face_it_is_showing(open_app):
    """Pressing the card adds the feature, and keeps doing so while it is
    showing its description.

    The description covers the card, so anything reachable only through the
    front is unreachable from the moment the pointer arrives -- which is every
    moment the card could be pressed. The one control on it that is NOT this
    decision has to stop the click, or reading "How it works" toggles the
    feature it explains; so does the pill, or the face's handler puts the
    feature straight back.
    """
    with open_app() as app:
        _open_setup(app)
        card = '.set-card[data-feature="places"]'

        chip = '#set-flow .set-chip[data-feature="places"]'

        def press(selector):
            app.hover(card)
            app.wait_shown(f"{card} .set-back")
            app.click(f"{card} {selector}")

        press(".set-back .set-card-detail")  # the face itself
        app.wait_for(chip)
        press(".set-back .set-add")  # the pill on it, exactly once
        assert app.count(chip) == 0
        press(".doc-more")  # the way out takes nothing with it
        app.wait_for("#docs")
        assert app.count(chip) == 0
        assert app.errors() == []


def test_only_the_card_under_the_pointer_shows_its_description(open_app):
    """One card turns over, not the shelf. Nothing records which -- it follows
    the pointer and is only ever CSS -- so what is worth checking is that the
    rule is anchored to the card: one level too high turns all eight over at
    once, and every card on the screen then says the same thing.
    """
    with open_app() as app:
        _open_setup(app)

        app.hover('.set-card[data-feature="semantic"]')
        app.wait_shown('.set-card[data-feature="semantic"] .set-back')

        shown = app.tab.evaluate(
            "[...document.querySelectorAll('#set-shelf .set-card')]"
            ".filter(c => getComputedStyle(c.querySelector('.set-back')).visibility"
            " === 'visible').map(c => c.dataset.feature)"
        )
        assert shown == ["semantic"]
        assert app.errors() == []


def test_choosing_one_of_a_pair_says_what_it_costs(open_app):
    """People and Pets check each other's work; the panel says so rather than
    letting the accuracy quietly drop."""
    with open_app() as app:
        _open_setup(app)

        app.click('.set-card[data-feature="people"] .set-add')
        app.wait_for(".set-pair")

        assert "Pets" in app.text(".set-pair")
        app.click('.set-card[data-feature="pets"] .set-add')
        assert app.count(".set-pair") == 0, "with both on there is nothing to warn about"


def test_a_name_typed_for_one_folder_does_not_follow_the_next_one(open_app):
    """The panel is hidden on close, not destroyed, so its markup outlives the
    visit that built it. Reading the name back off that field is how the second
    archive someone added arrived pre-named after the first."""
    with open_app() as app:
        _open_setup(app, "/tmp/first-folder")
        _type_name(app, "Holidays")
        app.tab.evaluate("closeArchiveSetup()")

        _open_setup(app, "/tmp/second-folder")

        assert _name(app) == ""
        # Empty means "follow the folder", which is what the placeholder says.
        assert (
            app.tab.evaluate("document.getElementById('setup-name').placeholder") == "second-folder"
        )
        assert app.errors() == []


def test_a_half_typed_name_survives_adding_a_feature(open_app):
    """Adding one rebuilds the whole panel. Losing what someone had typed
    because they then chose Places is the same small betrayal as a card turning
    itself back over."""
    with open_app() as app:
        _open_setup(app)
        _type_name(app, "Holidays")

        app.click('.set-card[data-feature="places"] .set-add')
        app.wait_for('#set-flow .set-chip[data-feature="places"]')

        assert _name(app) == "Holidays"
        assert app.errors() == []


def test_the_nav_only_offers_what_the_archive_runs(open_app, archive):
    """A feature that is off does not appear as a disabled or empty screen —
    the nav item is not there at all."""
    with open_app("overview") as app:
        labels = app.tab.evaluate(
            "[...document.querySelectorAll('#navitems .navitem')].map(e => e.title)"
        )
        assert {"People", "Pets", "Places"} <= set(labels)

    _configure(archive, features=["index", "duplicates", "places"])

    with open_app("overview") as app:
        labels = app.tab.evaluate(
            "[...document.querySelectorAll('#navitems .navitem')].map(e => e.title)"
        )
        assert "Places" in labels
        assert "People" not in labels and "Pets" not in labels
        # The ones every archive has are untouched.
        assert {"Overview", "Browse", "Timeline", "Duplicates"} <= set(labels)
        assert app.errors() == []


def test_browse_only_offers_description_search_to_an_archive_that_runs_it(open_app, archive):
    """The one feature that unlocks no section of its own: Search by
    description lives inside Browse, so switching it off cannot be expressed by
    dropping a nav item and has to be expressed here.

    What switching it off removes is one *way* of searching, not the box: the
    row promising to match what is in the frame goes, and so does the offer to
    describe a photo. The box stays, because matching what you type against
    file names needs no index and no model, and asks nothing of a stage the
    scheduler will never start."""
    with open_app("library", wait_for=".way") as app:
        assert app.count(".semantic-composer") == 1
        ways = app.tab.evaluate(
            "[...document.querySelectorAll('.way-text b')].map(e => e.textContent)"
        )
        # The catalogue's own name for it, which is what the setup panel it was
        # chosen on, the Overview card and its documentation page all use.
        assert "Search by description" in ways
        assert app.count("#group-media") == 1
        assert app.errors() == []

    _configure(archive, features=["index", "duplicates", "places"])

    with open_app("library", wait_for=".way") as app:
        assert app.count(".semantic-composer") == 1
        placeholder = app.tab.evaluate("document.getElementById('semantic-q').dataset.placeholder")
        assert "what your photos show" not in placeholder
        assert placeholder == "Search your library by filename"
        ways = app.tab.evaluate(
            "[...document.querySelectorAll('.way-text b')].map(e => e.textContent)"
        )
        assert ways == ["Search by filenamealways"], "one way left, and it says it is always there"
        # The rest of the screen is untouched: this changes what a search is
        # matched against, not the ways of looking through the archive.
        assert app.count("#filterbar") == 1
        assert app.count("#grid") == 1
        assert app.errors() == []


def _search_for(app, text):
    app.tab.evaluate(
        "(() => { const c = document.getElementById('semantic-q');"
        f" c.textContent = {text!r};"
        " c.closest('form').dispatchEvent("
        "new Event('submit', {cancelable: true, bubbles: true})); })()"
    )


def test_browse_searches_file_names_when_it_has_no_index_to_search(open_app, archive):
    """The floor under Browse's search box, driven the way a person drives it.

    Nothing in this archive is embedded or read here -- the feature that would
    do either is off -- so a single tile coming back proves the words were
    matched against the names the scan already recorded, and that the box is
    wired to the endpoint that can answer it."""
    _configure(archive, features=["index", "duplicates", "places"])

    with open_app("library", wait_for=".tile") as app:
        _search_for(app, "photo003")
        app.tab.wait_for(
            "document.querySelectorAll('#grid-name .tile').length === 1",
            what="the name group to narrow to the one file named photo003",
        )

        assert "photo003.jpg" in app.text("#grid-name")
        assert app.count("#grid-name mark") > 0, "the part of the name that matched is marked"
        # No ranking to sort by and none to widen: a name is matched or it is
        # not, so neither control that belongs to the description search
        # appears alongside it.
        assert app.count(".aq-scope") == 0
        assert "Best match" not in app.text("#f-sort")
        assert app.errors() == []


def test_an_archive_that_only_reads_pictures_can_still_search_what_it_read(open_app, archive):
    """The picture half filled the same index as the document half and had no way
    to reach it: the group that searches it was built only for archives that chose
    documents, so an archive reading its pictures indexed them and was never shown
    anywhere to look. Both readers are halves of one feature's index, so
    either one alone has to bring the group with it."""
    _configure(archive, features=["index", "duplicates", "ocr"])

    with open_app("library", wait_for=".way") as app:
        ways = app.tab.evaluate(
            "[...document.querySelectorAll('.way-text b')].map(e => e.textContent)"
        )
        # Named after the half that is actually on -- not the pair, and not a
        # wording of Browse's own.
        assert ways == ["Search by filenamealways", "Search by picture text"]
        # ...and it carries that half's mark, where it used to draw the document
        # page over a group full of photographs.
        said = app.text(".way:nth-child(2)")
        assert "screenshots, photos and scanned PDFs" in said
        assert "documents" not in said, "this archive was never promised its documents"

        _search_for(app, "lease")
        app.wait_for("#grid-text .tile")
        assert app.count("#group-text[hidden]") == 0
        assert app.errors() == []


def test_a_file_stays_findable_by_name_once_description_search_is_on(open_app, archive):
    """The regression this pair of groups exists to prevent.

    The two used to be alternatives -- with a description index the query went
    there *instead*, and the name filter was never sent -- so switching on
    Search by description silently took away the ability to find a file by its
    name, and a query like this one scored below the relevance floor and came
    back with nothing at all."""
    _configure(archive, features=["index", "duplicates", "semantic"])

    with open_app("library", wait_for=".tile") as app:
        _search_for(app, "photo003")
        app.tab.wait_for(
            "document.querySelectorAll('#grid-name .tile').length === 1",
            what="the name group to find photo003 even with description search on",
        )
        assert "photo003.jpg" in app.text("#grid-name")
        assert app.errors() == []


def test_a_link_to_a_switched_off_screen_lands_on_the_overview(open_app, archive):
    """A bookmark from before the feature was switched off, or a hash typed by
    hand. Rendering People with no people is a screen that never finishes."""
    _configure(archive, features=["index", "duplicates"])

    with open_app("people") as app:
        assert app.hash().endswith("/overview")
        assert app.active_nav() == "Overview"
        assert app.errors() == []
