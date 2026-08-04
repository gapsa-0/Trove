"""The archive setup screen, driven in a real browser.

Five things here have no other coverage and each fails silently:

* the screen renders at all -- it is built from a template literal, so a typo
  produces an empty panel rather than an error;
* dragging a card onto the pipeline does the same thing as pressing Add, and
  dragging a link back out does the same thing as pressing its remove button,
  which is the accessibility promise the screen is built on;
* turning a card over to read what a feature does leaves the grid where it was,
  which is the whole reason the two faces live in one fixed-height card;
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


def _open_setup(app, path="/tmp/example-folder", archive="null"):
    """Open setup the way the picker does: for a not-yet-created archive, or
    (with ``archive`` as a registry entry) for one being reconfigured.

    Waits on the path the panel was opened *for*, not on the panel: closing
    setup only hides it, so its last visit's markup is still in the DOM and
    anything that merely waits for a chip sails straight through the render.
    """
    app.tab.evaluate(
        f"import('/static/js/setup.js').then(m => m.openArchiveSetup({archive}, {path!r}))"
    )
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
        # Every optional feature keeps a card, chosen or not: the card is the
        # catalogue entry, and the chain is what was picked from it.
        assert app.count(".set-card") >= 3
        assert "0 MB" in app.text(".set-total")
        assert app.errors() == []


def test_dragging_a_card_onto_the_pipeline_adds_it(open_app):
    with open_app() as app:
        _open_setup(app)

        lit = _drag(app, '.set-card[data-feature="semantic"]', "#set-pipe")
        app.wait_for('#set-flow .set-chip[data-feature="semantic"]')

        assert lit == 1, "only the target that would change something is offered"
        assert "689 MB" in app.text(".set-total")
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
        app.click('.set-card[data-feature="people"] .set-flip')
        app.wait_for('.set-card[data-feature="people"] .set-back:not([hidden])')
        after = app.tab.evaluate(
            "[...document.querySelectorAll('.set-card')].map(e => Math.round("
            "e.getBoundingClientRect().height))"
        )

        assert before == after, "turning a card over resized the grid"
        assert len(set(after)) == 1, "the cards are not all the same height"
        # The description is the one from features.py, not the tagline again.
        assert "faces" in app.text('.set-card[data-feature="people"] .set-card-detail')
        assert app.errors() == []


def test_a_turned_card_stays_turned_when_the_shelf_is_rebuilt(open_app):
    """Adding a feature re-renders every card. Losing the page someone was
    reading because they pressed Add elsewhere is its own small betrayal."""
    with open_app() as app:
        _open_setup(app)

        app.click('.set-card[data-feature="semantic"] .set-flip')
        app.wait_for('.set-card[data-feature="semantic"] .set-back:not([hidden])')
        app.click('.set-card[data-feature="places"] .set-add')
        app.wait_for('#set-flow .set-chip[data-feature="places"]')

        assert app.count('.set-card[data-feature="semantic"] .set-back:not([hidden])') == 1
        assert app.count('.set-card[data-feature="places"] .set-back:not([hidden])') == 0
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


_EXISTING = (
    "{id: 1, path: '/tmp/example-folder', name: 'Old name', features: ['index', 'duplicates']}"
)


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


def test_renaming_an_archive_survives_adding_a_feature_too(open_app):
    """The same field on the same screen: a rename that reverted to the stored
    name the moment a feature was added is the same bug, from the other end."""
    with open_app() as app:
        _open_setup(app, "/tmp/example-folder", archive=_EXISTING)
        assert _name(app) == "Old name"

        _type_name(app, "New name")
        app.click('.set-card[data-feature="places"] .set-add')
        app.wait_for('#set-flow .set-chip[data-feature="places"]')

        assert _name(app) == "New name"
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
    dropping a nav item and has to be expressed here. Left ungated, the
    composer invited a search of an index whose stage the scheduler never
    starts, over a line promising files "queued for indexing"."""
    with open_app("library", wait_for=".library-controls") as app:
        assert app.count(".semantic-composer") == 1
        assert app.errors() == []

    _configure(archive, features=["index", "duplicates", "places"])

    with open_app("library", wait_for=".library-controls") as app:
        assert app.count(".semantic-composer") == 0
        assert app.count(".search-reach") == 0
        # The rest of the screen is untouched: this removes a search, not a way
        # of looking through the archive.
        assert app.count("#filterbar") == 1
        assert app.count("#grid") == 1
        assert app.errors() == []


def test_a_link_to_a_switched_off_screen_lands_on_the_overview(open_app, archive):
    """A bookmark from before the feature was switched off, or a hash typed by
    hand. Rendering People with no people is a screen that never finishes."""
    _configure(archive, features=["index", "duplicates"])

    with open_app("people") as app:
        assert app.hash().endswith("/overview")
        assert app.active_nav() == "Overview"
        assert app.errors() == []
