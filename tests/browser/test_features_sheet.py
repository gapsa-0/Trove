"""The Features sheet, driven in a real browser.

What this archive runs used to be changeable only from the start page, on the
screen the archive was created with. It is changed from inside the archive now,
and the sheet that does it is a fork of that screen rather than a second view of
it -- so the things worth pinning here are the ones that make it a fork:

* the Library health panel offers a way to open it, which is the only thing in
  an open archive that says the features it does *not* run exist at all;
* every card carries one fact -- what the feature found here, or what it would
  cost to switch on -- and that fact does not move when its switch is flipped,
  because flipping a switch changes a plan and not a result;
* a card turns over to what the feature does, and the grid does not move when
  it does, which is the promise the shared fixed-height card is built on;
* nothing happens until Save, and Save is inert until there is something to
  save;
* saving really changes what the archive runs, which the nav has to show;
* reading a feature's page from a card comes back to the sheet with the
  switches as they were left, rather than to an empty archive.
"""

from __future__ import annotations

import json
import time
import urllib.request


def _configure(archive, **body):
    """Change the archive's setup through the API the sheet itself posts to."""
    request = urllib.request.Request(
        f"{archive.base_url}/api/archive/configure",
        data=json.dumps({"root_id": archive.root_id, **body}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read())


def _open_sheet(app):
    """Open it the way a person does: from the foot of the Library health panel."""
    app.click(".manage-features")
    # The sheet fetches the catalogue and the text index before it can draw a
    # card, so wait for a card rather than for the sheet.
    app.wait_for(".fsheet.open .fcard")


def _fact(app, feature):
    return app.text(f'.fcard[data-feature="{feature}"] .set-cost')


def _switch(app, feature):
    return app.tab.evaluate(
        f"(document.querySelector('.fcard[data-feature=\"{feature}\"] .fsw') || {{}})"
        ".getAttribute?.('aria-checked')"
    )


def _flip(app, feature):
    app.click(f'.fcard[data-feature="{feature}"] .fsw')


def _press_card(app, feature):
    """Press the card itself, which is the other way to switch a feature."""
    app.click(f'.fcard[data-feature="{feature}"] .set-face:not([hidden])')


def _nav_labels(app):
    return app.tab.evaluate(
        "[...document.querySelectorAll('#navitems .navitem')].map(e => e.title)"
    )


def test_the_health_panel_offers_a_way_to_change_what_it_runs(open_app):
    """A feature that is off has no card on the panel at all -- stages.py leaves
    it out rather than reporting it as "off" -- so without this nothing in an
    open archive says the others exist.

    Under the chain rather than on it: the rail is a report on what is running,
    and a node that opens a screen instead of reporting a stage is not that.
    """
    with open_app("overview") as app:
        app.wait_for(".status-panel .panel-foot .manage-features")
        assert "Manage features" in app.text(".manage-features")
        assert app.count(".health-grid .manage-features") == 0

        _open_sheet(app)

        # One card per feature in the catalogue, in the catalogue's own order,
        # which is the order of the chain in the panel this was opened from.
        listed = app.tab.evaluate(
            "[...document.querySelectorAll('.fcard')].map(e => e.dataset.feature)"
        )
        assert listed[:2] == ["index", "duplicates"]
        assert len(listed) >= 6
        # The two the archive cannot decline say so instead of offering a switch
        # that would refuse to move.
        assert app.count('.fcard[data-feature="index"] .fsw') == 0
        assert "Always runs" in app.text('.fcard[data-feature="index"]')
        assert app.errors() == []


def test_a_card_turns_over_to_what_the_feature_does_without_moving_the_grid(open_app):
    """The promise the shared fixed-height card is built on, kept on this side of
    the fork too: reading one description must not shuffle the catalogue around
    it. And behind every description is the way out to that feature's page."""
    with open_app("overview") as app:
        _open_sheet(app)
        before = app.tab.evaluate(
            "[...document.querySelectorAll('.fcard')].map(e => Math.round("
            "e.getBoundingClientRect().height))"
        )

        app.hover('.fcard[data-feature="people"]')
        app.wait_shown('.fcard[data-feature="people"] .set-back')

        after = app.tab.evaluate(
            "[...document.querySelectorAll('.fcard')].map(e => Math.round("
            "e.getBoundingClientRect().height))"
        )
        assert before == after, "turning a card over resized the grid"
        assert len(set(after)) == 1, "the cards are not all the same height"
        # The description from features.py, not the tagline again.
        assert "faces" in app.text('.fcard[data-feature="people"] .set-card-detail')
        # Every feature has somewhere to read more, which is what lets the card
        # carry one paragraph rather than the whole story.
        assert app.count(".fcard .doc-more") == app.count(".fcard")
        assert app.errors() == []


def test_a_running_feature_reports_what_it_found_and_a_stopped_one_what_it_costs(open_app, archive):
    """The one column this screen exists for. At create time it could only ever
    hold prices; here it holds prices for what is off and results for what is
    on, which is what makes the two screens different screens."""
    _configure(archive, features=["index", "duplicates", "places"])

    with open_app("overview") as app:
        _open_sheet(app)

        # On: what this archive got out of it. Both "8 photos placed" and
        # "No places found" are answers; a count of megabytes is not.
        assert "catalogued" in _fact(app, "index")
        assert "place" in _fact(app, "places")
        # Off: what switching it on would cost, in the same three words the
        # setup screen uses for it.
        semantic = _fact(app, "semantic")
        assert semantic in {"No download needed", "Downloaded", "715 MB"}, semantic
        # ...and says which of the three it is in colour as well as in words,
        # since only one of them is a reason to hesitate.
        tone = app.tab.evaluate(
            "document.querySelector('.fcard[data-feature=\"semantic\"] .set-cost').className"
        )
        # "set-cost" alone is the fourth answer: a feature whose backend is not
        # installed quotes its size without pricing a download it cannot start.
        assert tone in {"set-cost free", "set-cost ready", "set-cost cost", "set-cost"}, tone
        # A running feature's fact is a count, not one of those three answers,
        # so it borrows none of their colours.
        assert (
            app.tab.evaluate(
                "document.querySelector('.fcard[data-feature=\"places\"] .set-cost').className"
            )
            == "set-cost"
        )
        assert app.errors() == []


def test_a_cards_fact_does_not_move_when_its_switch_is_flipped(open_app, archive):
    """Flipping a switch changes what the archive *will* run. What Places has
    already found does not change until it is saved, and pretending otherwise
    would hide the very thing being given up."""
    _configure(archive, features=["index", "duplicates", "places"])

    with open_app("overview") as app:
        _open_sheet(app)
        before = _fact(app, "places")

        _flip(app, "places")

        assert _switch(app, "places") == "false"
        assert _fact(app, "places") == before
        # ...and now there is something to say about what happens to it.
        assert app.count("#fsheet-kept[hidden]") == 0
        assert "keeps everything it found" in app.text("#fsheet-kept")
        assert app.errors() == []


def test_every_description_is_shown_whole(open_app):
    """The other half of ``features.DETAIL_MAX_WORDS``.

    A description is longer than the card it belongs to -- deliberately, since
    the card is sized for the face it actually shows -- so it lifts off the card
    and runs past its bottom edge instead of being folded into it. The promise
    that replaces "it fits" is "none of it is hidden", and it can be broken just
    as silently: a card that clips its overflow again, or a panel pinned to the
    card's height, cuts the end off the paragraph that answers "should I turn
    this on".
    """
    with open_app("overview") as app:
        _open_sheet(app)

        # One card at a time, actually hovered: the card only stops clipping
        # while it is the one being read, so measuring them all at rest reports
        # eight cut-off descriptions and proves nothing about any of them.
        clipped = []
        for feature in app.tab.evaluate(
            "[...document.querySelectorAll('.fcard')].map(c => c.dataset.feature)"
        ):
            card = f'.fcard[data-feature="{feature}"]'
            app.hover(card)
            app.wait_shown(f"{card} .set-back")
            cut, clip = app.tab.evaluate(
                f"(() => {{ const c = document.querySelector({card!r});"
                "   const back = c.querySelector('.set-back');"
                # The panel must be as tall as its own contents (nothing
                # scrolling away inside it) *and* the card must not be cutting
                # it off, which a height alone cannot see.
                "   return [back.scrollHeight - back.clientHeight,"
                "           getComputedStyle(c).overflow !== 'visible']; })()"
            )
            if cut > 0 or clip:
                clipped.append(
                    feature
                    + (" (clipped by the card)" if clip else "")
                    + (f" (+{cut}px)" if cut > 0 else "")
                )

        assert clipped == [], (
            f"descriptions are cut off: {', '.join(clipped)}. The panel has to be free to "
            "run past the card (see .set-back in setup.css), or the descriptions have to "
            "shrink (features.DETAIL_MAX_WORDS)."
        )
        assert app.errors() == []


def test_pressing_the_card_switches_it_and_the_two_controls_on_it_do_not(open_app, archive):
    """A 40px switch is a small target for a decision this size, so the whole
    front of the card is one -- which is exactly how a card ends up toggling
    twice and landing back where it started, if the switch on it lets its own
    click through."""
    _configure(archive, features=["index", "duplicates", "places"])

    with open_app("overview") as app:
        _open_sheet(app)

        _press_card(app, "places")
        assert _switch(app, "places") == "false", "pressing the card did not switch it"

        _press_card(app, "places")
        assert _switch(app, "places") == "true", "and back again"

        # The switch itself switches it once, not twice.
        _flip(app, "places")
        assert _switch(app, "places") == "false"

        # "More info" turns the card over and leaves the feature alone.
        app.hover('.fcard[data-feature="places"]')
        app.wait_shown('.fcard[data-feature="places"] .set-back')
        assert _switch(app, "places") == "false", "reading about it changed it"
        assert app.errors() == []


def test_a_card_with_nothing_to_switch_is_not_pressable(open_app):
    """Indexing cannot be turned off. A card that lifts to meet the pointer is
    promising a press that would do nothing."""
    with open_app("overview") as app:
        _open_sheet(app)

        assert app.count('.fcard[data-feature="index"].fcard-fixed-face') == 1
        _press_card(app, "index")

        assert app.count('.fcard[data-feature="index"].on') == 1, "Indexing was switched off"
        assert app.tab.evaluate("document.getElementById('fsheet-save').disabled") is True
        assert app.errors() == []


def test_nothing_is_saved_until_there_is_something_to_save(open_app, archive):
    """The sheet opens inert. Every switch on it is a change of plan, and until
    one moves there is no plan to commit."""
    _configure(archive, features=["index", "duplicates", "places"])

    with open_app("overview") as app:
        _open_sheet(app)
        assert app.tab.evaluate("document.getElementById('fsheet-save').disabled") is True
        assert "Nothing to download" in app.text("#fsheet-total")

        _flip(app, "places")
        assert app.tab.evaluate("document.getElementById('fsheet-save').disabled") is False

        # Back where it started is not a change, however many switches it took.
        _flip(app, "places")
        assert app.tab.evaluate("document.getElementById('fsheet-save').disabled") is True
        assert app.count("#fsheet-kept[hidden]") == 1
        assert app.errors() == []


def test_saving_changes_what_the_archive_runs(open_app, archive):
    """The whole point, end to end: the sheet closes, and the archive around it
    is the one the switches described."""
    _configure(archive, features=["index", "duplicates", "places"])

    with open_app("overview") as app:
        assert "Places" in _nav_labels(app)
        _open_sheet(app)
        _flip(app, "places")

        app.click("#fsheet-save")

        app.tab.wait_for(
            "document.querySelectorAll('#navitems .navitem').length > 0"
            " && ![...document.querySelectorAll('#navitems .navitem')]"
            ".some(e => e.title === 'Places')",
            what="the nav to lose the section Places was unlocking",
        )
        assert app.count(".fsheet.open") == 0
        # The screen underneath was rebuilt from the new answer rather than
        # resumed from a fragment that predates it.
        app.wait_for(".manage-features")
        assert app.errors() == []


def test_reading_a_features_page_comes_back_to_the_sheet(open_app, archive):
    """The sheet is a fixed layer over the archive, so hiding the archive does
    not hide it: a feature's page used to be reachable from a row, and would
    have opened underneath its own sheet."""
    _configure(archive, features=["index", "duplicates", "places"])

    with open_app("overview") as app:
        _open_sheet(app)
        _flip(app, "places")

        app.hover('.fcard[data-feature="places"]')
        app.wait_shown('.fcard[data-feature="places"] .set-back')
        app.click('.fcard[data-feature="places"] .doc-more')
        app.wait_for("#docs.on .doc-body, #docs.on article, #docs.on")
        assert app.count(".fsheet.open") == 0, "the page opened under the sheet"

        app.tab.evaluate("closeDocs()")

        app.wait_for(".fsheet.open .fcard")
        # ...with the decision that was half made still half made.
        assert _switch(app, "places") == "false"
        assert app.errors() == []


def test_a_cards_description_fits_the_card_it_comes_out_of(open_app):
    """Turning a card over must not spill it onto the ones underneath.

    The shelf these cards are forked from lets a description hang past the
    bottom edge, which buys a shorter card there. Here the card is 142px and
    the descriptions run past 240, so most of what you were reading sat on top
    of the row below -- a panel two-thirds larger than the thing it came out of.

    Asserted against the card rather than against a number, so a description
    added tomorrow that needs more room grows the card instead of failing.
    """
    with open_app("overview") as app:
        _open_sheet(app)
        spills = app.tab.evaluate(
            "[...document.querySelectorAll('.fsheet .set-card.fcard')].map(c => {"
            " const b = c.querySelector('.set-back');"
            " return {name: (c.querySelector('.set-card-name') || {}).textContent.trim(),"
            "         over: b.scrollHeight - c.getBoundingClientRect().height}; })"
            ".filter(r => r.over > 0)"
        )
        assert spills == [], f"descriptions taller than their cards: {spills}"
        assert app.errors() == []


def test_every_card_in_the_sheet_is_the_same_height(open_app):
    """...and they are levelled by the tallest, so turning one over moves none
    of the others -- the promise the fixed height used to keep."""
    with open_app("overview") as app:
        _open_sheet(app)
        heights = app.tab.evaluate(
            "[...document.querySelectorAll('.fsheet .set-card.fcard')]"
            ".map(c => Math.round(c.getBoundingClientRect().height))"
        )
        assert len(set(heights)) == 1, f"cards are ragged: {sorted(set(heights))}"


def _settled_card_shot(app, box) -> str:
    """A PNG of that rectangle, once it has stopped changing.

    The card lifts under the pointer and its description fades in, both on
    transitions, so a shot taken the moment the panel turns visible catches it
    mid-animation. Two identical captures in a row is the settled state, and the
    rectangle is measured once by the caller so both shots frame the same pixels.
    """
    shot = lambda: app.tab.call(  # noqa: E731
        "Page.captureScreenshot", {"format": "png", "clip": box}
    )["result"]["data"]
    previous = shot()
    for _ in range(60):
        time.sleep(0.05)
        current = shot()
        if current == previous:
            return current
        previous = current
    raise AssertionError("the card never stopped changing")


def test_the_description_covers_the_front_it_is_drawn_over(open_app):
    """Both faces carry the fact and the switch on purpose (features.js): the
    description covers the card, and a switch printed only on the front would
    vanish the moment you pointed at it. That only works while the description
    is genuinely on top of the front.

    In flow with no stacking context of its own it is not, and not in a way
    hit-testing shows: a background paints in an earlier phase than the CONTENT
    of its siblings, so the front's switch came up through the description and
    one card showed two knobs, one live and one dead. `elementFromPoint` names
    the description at that spot either way, which is why this compares ink.

    The front is hidden rather than removed so the card is laid out identically
    in both shots and the only thing that can differ is what the front drew.
    """
    with open_app("overview") as app:
        _open_sheet(app)
        card = '.fcard[data-feature="people"]'
        app.hover(card)
        app.wait_shown(f"{card} .set-back")
        box = app.tab.evaluate(
            f"(() => {{ const r = document.querySelector({card!r}).getBoundingClientRect();"
            " return {x: r.x, y: r.y, width: r.width, height: r.height, scale: 1}; })()"
        )
        with_front = _settled_card_shot(app, box)
        app.tab.evaluate(
            f"document.querySelector('{card} .set-face:not(.set-back)').style.visibility = 'hidden'"
        )
        without_front = _settled_card_shot(app, box)

        assert with_front == without_front, "the front draws through the description covering it"
        assert app.errors() == []
