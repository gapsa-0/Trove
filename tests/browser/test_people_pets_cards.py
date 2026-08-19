"""A group's card as a control surface: its menu, its drag, and its place.

Split from test_people_pets_edit.py, which had grown to hold two jobs. That one
is about giving a group a name; this one is about everything else a card is --
the ⋯ menu and the two ways out of the grid it offers, dragging one card onto
another to merge them, the photos on a group's own page, the record of what you
changed, and the grid holding its position while you step into a group and back.

Driven through the People screen where the two screens share a control, and
through Pets where the point is that it has one too.
"""

from __future__ import annotations


def test_hiding_a_person_as_unknown_moves_them_into_the_hidden_section(open_app):
    """The whole round trip, through the controls, in one test: the menu opens,
    the card leaves the grid, the Unknown section appears with it inside, and
    putting it back returns it. Each half is useless without the other."""
    with open_app("people") as app:
        app.wait_for_text("Ada")
        before = app.count(".pcard")
        app.click(".pcard .cardmenu-trigger")
        app.wait_for(".cardmenu-panel .cardmenu-item")
        _assert_menu_usable(app)
        # By label, never by position: "Not a person" opens a confirm dialog,
        # which blocks the tab outright, so picking the wrong item here does
        # not fail the test -- it hangs the browser.
        _pick_menu_item(app, "Unknown person")
        app.tab.wait_for(
            f"document.querySelectorAll('#peoplegrid .pcard').length === {before - 1}",
            timeout=10.0,
            what="the hidden card to leave the grid",
        )
        app.wait_for(".hidden-people")
        assert "Unknown" in app.text("#hiddenwrap")

        app.tab.evaluate("document.querySelector('.hidden-people').setAttribute('open', '')")
        app.wait_for(".hidden-people .pcard.is-hidden")
        app.click(".hidden-people .pcard.is-hidden .quietbtn")
        app.tab.wait_for(
            f"document.querySelectorAll('#peoplegrid .pcard').length === {before}",
            timeout=10.0,
            what="the restored card to come back to the grid",
        )
        assert app.errors() == []


def _type_into(app, selector: str, value: str) -> None:
    """Put a value in a field and commit it the way clicking away does.

    The editor saves on blur (``static/js/nameedit.js``). The event is
    dispatched rather than ``i.blur()`` called, because a tab that does not
    hold the window's focus never gave the input focus to begin with, so
    ``blur()`` is a no-op there and nothing would ever save.
    """
    app.tab.evaluate(
        f"(() => {{ const i = document.querySelector({selector!r});"
        f" i.value = {value!r}; i.dispatchEvent(new Event('blur')); }})()"
    )


def _pick_menu_item(app, label: str) -> None:
    """Click an open card menu's item by its words.

    Position would be brittle and, worse, dangerous: "Not a person" raises a
    confirm dialog, and a browser modal blocks every later CDP command rather
    than failing an assertion.
    """
    clicked = app.tab.evaluate(
        "(() => { const b = [...document.querySelectorAll('.cardmenu-panel .cardmenu-item')]"
        f".find(b => b.textContent.includes({label!r}));"
        " if (!b) return false; b.click(); return true; })()"
    )
    assert clicked, f"no menu item saying {label!r}"


def _assert_menu_usable(app) -> None:
    """The menu is on screen and nothing is covering it.

    Presence in the DOM proves nothing here, and that is not hypothetical: the
    panel first shipped nested inside a card that clips its own overflow and
    lifts on hover with a transform, so every item existed, none could be seen,
    and a test asserting existence passed the whole time. elementFromPoint is
    what tells the difference -- it answers with whatever is actually painted
    at the centre of the first item.
    """
    verdict = app.tab.evaluate(
        "(() => { const item = document.querySelector('.cardmenu-panel .cardmenu-item');"
        " if (!item) return 'no menu items';"
        " const r = item.getBoundingClientRect();"
        " if (r.width < 40 || r.height < 10) return 'menu item has no size: ' + JSON.stringify(r);"
        " if (r.top < 0 || r.bottom > innerHeight || r.left < 0 || r.right > innerWidth)"
        "   return 'menu is off screen: ' + JSON.stringify(r);"
        " const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);"
        " if (!hit || !hit.closest('.cardmenu-panel'))"
        "   return 'something covers the menu: ' + (hit ? hit.className : 'nothing');"
        " return 'ok'; })()"
    )
    assert verdict == "ok", verdict


def _avatar_face(app):
    """Which face the person page's avatar is currently drawing."""
    return app.tab.evaluate(
        "(() => { const a = document.querySelector('.person-header-avatar');"
        " const m = a && a.src && a.src.match(/faceThumb\\/(\\d+)/);"
        " return m ? Number(m[1]) : null; })()"
    )


def test_a_photo_on_a_persons_page_carries_its_own_actions(open_app):
    """Both controls exist on the tile, and choosing a cover *sticks*.

    Asserting the avatar is "a face thumbnail" proves nothing -- it always was
    one. The assertion has to be that it is the face that was chosen, and still
    is after leaving the page and coming back, because the bug this guards
    against wrote the choice to the database and drew it from somewhere else.
    """
    with open_app("people") as app:
        app.wait_for_text("Ada")
        app.click(".pcard img.face, .pcard .facecollage img")
        app.wait_for(".tile")
        assert app.count(".tile .tile-detach") >= 1, "the detach control went missing"
        assert app.count(".tile .tile-cover") >= 1, "no way to choose a cover"

        # The LAST photo with a face, so the chosen cover is not also whatever
        # the page would have drawn anyway -- which is exactly the confusion
        # the old code hid behind.
        before = _avatar_face(app)
        chosen = app.tab.evaluate(
            "(() => { const t = [...document.querySelectorAll('.tile')]"
            ".filter(t => t.querySelector('.tile-cover')).pop();"
            " t.querySelector('.tile-cover').click(); return Number(t.dataset.fileId); })()"
        )
        assert chosen is not None
        app.tab.wait_for(
            "(() => { const a = document.querySelector('.person-header-avatar');"
            " const m = a && a.src && a.src.match(/faceThumb\\/(\\d+)/);"
            f" return m ? Number(m[1]) !== {before} : false; }})()",
            timeout=10.0,
            what="the header avatar to change to the chosen face",
        )
        after = _avatar_face(app)
        assert after != before

        # Leave and come back: the choice has to survive being re-read.
        # Scoped to the top bar: index.html has a `.back-control` of its own
        # that leaves the archive entirely, and it comes first in the document.
        app.click(".facetopbar .back-control")
        app.wait_for(".pcard")
        app.click(".pcard img.face, .pcard .facecollage img")
        app.wait_for(".tile")
        assert _avatar_face(app) == after, "the chosen cover did not survive a reload"
        assert app.errors() == []


def test_merge_with_offers_the_named_groups_and_merges_into_one(open_app):
    """Drag-to-merge needs both cards on screen; this is for the case a grid of
    hundreds makes common, where the group you recognise is nowhere near."""
    with open_app("people") as app:
        app.wait_for_text("Ada")
        before = app.count(".pcard")
        app.click(".pcard .cardmenu-trigger")
        app.wait_for(".cardmenu-panel .cardmenu-item")
        _assert_menu_usable(app)
        # It replaces the menu with the list rather than closing, so the menu
        # is still open afterwards.
        _pick_menu_item(app, "Merge with")
        app.wait_for(".cardmenu-panel .cardmenu-item[data-id]")
        _assert_menu_usable(app)
        offered = app.tab.evaluate(
            "[...document.querySelectorAll('.cardmenu-panel .cardmenu-item[data-id]')]"
            ".map(b => b.textContent)"
        )
        assert offered and all(offered), "only named groups, each with a name to show"
        assert app.errors() == []
        # The card being merged from is not one of its own options.
        assert len(offered) < before or before == 1


def test_a_pets_photos_carry_the_same_actions_a_persons_do(open_app):
    """The Pets screen was a generation behind: one thumbnail per card and no
    per-photo controls at all."""
    with open_app("pets") as app:
        app.wait_for_text("Kira")
        app.click(".pcard img.face, .pcard .facecollage img")
        app.wait_for(".tile")
        assert app.count(".tile .tile-detach") >= 1, "no way to remove a photo from a pet"
        assert app.count(".tile .tile-cover") >= 1, "no way to choose a pet's cover"
        assert app.errors() == []


def test_a_persons_recent_changes_open_from_the_top_bar(open_app):
    """The merge list used to sit permanently between the name and the photos.

    Asserting it is *absent* until asked for is half the point of the change,
    so the first assertion is that the page opens on the faces.

    The second half has to be a real entry, made first so there is something to
    find. Accepting "either a row or the empty state" is what let this panel
    ship answering "No changes yet" to every question: the request was being
    refused for want of a ``root``, and an assertion that tolerates the empty
    state cannot tell a working panel from a broken one.
    """
    with open_app("people") as app:
        app.wait_for_text("Ada")
        # An edit worth finding again.
        app.click(".pcard .pname")
        app.wait_for(".pcard .pmeta-editing input")
        _type_into(app, ".pcard .pmeta-editing input", "Ada L")
        app.wait_for_text("Ada L")

        app.click(".pcard img.face, .pcard .facecollage img")
        app.wait_for("#histmenu")
        assert app.count(".hist-menu .hist-row") == 0, "the history should not be open on arrival"

        app.click("#histmenu > summary")
        app.wait_for(".hist-menu .hist-row")
        assert "Ada L" in app.text(".hist-menu"), "the rename just made should be listed"
        assert app.errors() == []

        # Escape dismisses it, through main.js's shared popover listeners.
        app.tab.evaluate(
            "document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))"
        )
        assert app.tab.evaluate("!!document.querySelector('#histmenu[open]')") is False


def _scroll_and_return(app, open_selector: str, back_selector: str) -> tuple[int, int]:
    """Scroll the grid, open a group, come back; report the scroll either side."""
    # A seeded archive has two cards, so there is nothing to scroll; a spacer
    # inside the scroller gives it somewhere to go. It rides along in the
    # stashed fragment, which is exactly what is under test.
    app.tab.evaluate(
        "(() => { const m = document.getElementById('main');"
        " const pad = document.createElement('div');"
        " pad.style.height = '1200px'; pad.id = 'scrollpad';"
        " m.appendChild(pad); m.scrollTop = 400; })()"
    )
    before = app.tab.evaluate("document.getElementById('main').scrollTop")
    app.click(open_selector)
    app.wait_for(back_selector)
    app.click(back_selector)
    app.wait_for(".pcard")
    # The position is put back in a requestAnimationFrame -- the nodes have to be
    # laid out before a scrollTop means anything (backToPeople) -- so reading it
    # straight after the cards appear races that frame. This used to "wait" on
    # `scrollTop > 0 || true`, which is true before anything has happened at all,
    # and the tests below duly failed whenever the machine was busy enough for
    # the frame to land after the read.
    app.tab.wait_for(
        f"document.getElementById('main').scrollTop === {before}",
        timeout=5.0,
        what="the grid's scroll position to be put back",
    )
    return before, app.tab.evaluate("document.getElementById('main').scrollTop")


def test_the_people_grid_keeps_its_place_while_you_open_someone(open_app):
    """Coming back rebuilt the screen, which dropped both the scroll position
    and every page the infinite list had loaded -- most of what you were
    looking at, on a screen of several hundred groups."""
    with open_app("people") as app:
        app.wait_for_text("Ada")
        app.wait_for(".pcard")
        before, after = _scroll_and_return(
            app, ".pcard img.face, .pcard .facecollage img", ".facetopbar .back-control"
        )
        assert before > 0, "the grid did not scroll, so this proves nothing"
        assert after == before, f"scroll reset: was {before}, came back {after}"
        assert app.errors() == []


def test_the_pets_grid_keeps_its_place_too(open_app):
    with open_app("pets") as app:
        app.wait_for_text("Kira")
        app.wait_for(".pcard")
        before, after = _scroll_and_return(
            app, ".pcard img.face, .pcard .facecollage img", ".facetopbar .back-control"
        )
        assert before > 0, "the grid did not scroll, so this proves nothing"
        assert after == before, f"scroll reset: was {before}, came back {after}"
        assert app.errors() == []


def _press_and_nudge(app, selector: str) -> None:
    """Press a control, travel past the drag threshold, release still inside it.

    An ordinary click: nobody presses a button without the mouse moving a few
    pixels. Driven through ``Input.dispatchMouseEvent`` rather than ``.click()``
    because the travel is the whole point -- a synthetic click never moves, so
    it can never be mistaken for the start of a drag.
    """
    x, y = app.tab.evaluate(
        f"(() => {{ const r = document.querySelector({selector!r}).getBoundingClientRect();"
        " return [r.x + r.width / 2, r.y + r.height / 2]; })()"
    )
    for kind, dx, dy in (
        ("mousePressed", 0, 0),
        ("mouseMoved", 4, 2),
        ("mouseMoved", 8, 3),
        ("mouseReleased", 8, 3),
    ):
        app.tab.call(
            "Input.dispatchMouseEvent",
            {
                "type": kind,
                "x": x + dx,
                "y": y + dy,
                "button": "left",
                "clickCount": 1,
                "buttons": 0 if kind == "mouseReleased" else 1,
            },
        )


def _watch_drags(app) -> None:
    app.tab.evaluate(
        "window.__drags = 0; addEventListener('dragstart', () => { window.__drags++; }, true);"
    )


def _drags(app) -> int:
    return app.tab.evaluate("window.__drags")


def _abandon_drag(app) -> None:
    """Escape out of the drag this test deliberately started.

    A drag is a browser-wide modal gesture, not a page one, and releasing the
    button through the input pipeline does not end it. Left in progress it
    outlives the tab, and the next test to open one gets a browser that will
    not play a video -- which cost an afternoon of blaming the video player.
    """
    for kind in ("keyDown", "keyUp"):
        app.tab.call(
            "Input.dispatchKeyEvent",
            {
                "type": kind,
                "key": "Escape",
                "code": "Escape",
                "windowsVirtualKeyCode": 27,
                "nativeVirtualKeyCode": 27,
            },
        )


def test_pressing_the_name_does_not_start_a_merge_drag(open_app):
    """The card is draggable for merge, and that used to swallow this click.

    Asserted on the drag rather than on the editor, and that is not a weaker
    check -- it is the only honest one this browser can make. A real Chrome
    that starts a drag enters a modal loop and delivers no click at all, which
    is the failure; headless synthetic input never enters that loop, so the
    editor opens here either way and an assertion about it would pass against
    the bug. That no drag begins is the same fact, observable.
    """
    with open_app("people") as app:
        app.wait_for(".pcard .pname")
        _watch_drags(app)
        _press_and_nudge(app, ".pcard .pname")
        app.wait_for(".pcard .pmeta-editing input")
        assert _drags(app) == 0, "pressing the name began a merge-drag of the card"
        assert app.errors() == []


def test_pressing_the_actions_menu_does_not_start_a_merge_drag(open_app):
    """The same gesture, on the other control every card carries."""
    with open_app("people") as app:
        app.wait_for(".pcard .cardmenu-trigger")
        _watch_drags(app)
        _press_and_nudge(app, ".pcard .cardmenu-trigger")
        app.wait_for(".cardmenu-panel .cardmenu-item")
        assert _drags(app) == 0, "pressing the actions menu began a merge-drag"
        assert app.errors() == []


def test_the_card_can_still_be_dragged_to_merge(open_app):
    """...and the press that is a grab still is one, or the fix above is a
    feature removal wearing a bug fix's clothes."""
    with open_app("people") as app:
        app.wait_for(".pcard img, .pcard .facecollage")
        _watch_drags(app)
        _press_and_nudge(app, ".pcard .facecollage, .pcard img")
        assert _drags(app) == 1, "a press on the card's picture no longer drags it"
        _abandon_drag(app)


def test_the_merge_list_can_be_scrolled_without_the_menu_shutting(open_app):
    """ "Merge with…" is the one list here long enough to need scrolling.

    The menu is pinned to a card's rectangle, so it closes when the screen
    scrolls out from under it rather than chasing it. That listener captures at
    the window, which sees a scroll of *any* element -- including the panel's
    own -- so scrolling the list shut it, and only the first few names were ever
    reachable.

    Scroll events are dispatched rather than provoked because the seeded archive
    has too few named groups to overflow the panel; what is under test is which
    scrolls the handler acts on, and that is exactly what this asks it.
    """
    with open_app("people") as app:
        app.wait_for(".pcard .cardmenu-trigger")
        app.click(".pcard .cardmenu-trigger")
        app.wait_for(".cardmenu-panel .cardmenu-item")
        app.tab.evaluate(
            "document.querySelector('.cardmenu-panel')"
            ".dispatchEvent(new Event('scroll', {bubbles: false}))"
        )
        assert app.count(".cardmenu-panel") == 1, "scrolling the menu closed it"
        app.tab.evaluate(
            "document.getElementById('main').dispatchEvent(new Event('scroll', {bubbles: false}))"
        )
        app.tab.wait_for(
            "document.querySelectorAll('.cardmenu-panel').length === 0",
            timeout=5.0,
            what="the menu to close when the screen behind it scrolls",
        )
        assert app.errors() == []


def _select_cards(app, n: int) -> list[str]:
    """Turn on selection and mark the first `n` cards, returning their keys."""
    app.click(".selectstart")
    app.wait_for("#peoplegrid.selecting")
    keys = app.tab.evaluate(
        "[...document.querySelectorAll('#peoplegrid .pcard')].map(c => c.dataset.syncKey)"
    )
    for key in keys[:n]:
        app.click(f'#peoplegrid .pcard[data-sync-key="{key}"]')
    return keys[:n]


def test_choosing_several_groups_does_not_open_any_of_them(open_app):
    """The card is a way into a group and, while selecting, a member of a set.
    It cannot be both: a click that opened the person would take the screen
    away mid-selection, which is the whole reason this is a mode."""
    with open_app("people") as app:
        app.wait_for("#peoplegrid .pcard")
        chosen = _select_cards(app, 2)

        assert len(chosen) == 2, "this needs two cards to choose between"
        assert app.count("#peoplegrid .pcard.is-selected") == 2
        assert app.count("#peoplegrid") == 1, "a card opened its group instead of being chosen"
        assert "2 people" in app.text("#selectbar")
        assert app.errors() == []


def test_a_set_of_one_cannot_be_merged(open_app):
    """Merging needs two. A button that says otherwise has to be pressed to
    find out, which is the worst way to be told."""
    with open_app("people") as app:
        app.wait_for("#peoplegrid .pcard")
        _select_cards(app, 1)

        assert app.tab.evaluate(
            "document.querySelector('#selectbar [data-act=\"merge\"]').disabled"
        ), "one group on its own was offered a merge"


def test_merging_a_selection_folds_them_into_one(open_app):
    """The point of the mode, and the reason it drives the same endpoint a
    single merge does: which name survives and which group does are rules with
    one home, not two."""
    with open_app("people") as app:
        app.wait_for("#peoplegrid .pcard")
        before = app.count("#peoplegrid .pcard")
        _select_cards(app, 2)

        app.click('#selectbar [data-act="merge"]')

        # Both seeded groups are named, and there is no automatic way to choose
        # between two things a person typed -- so the same dialog a drag-merge
        # raises asks which name stays, once for the whole set rather than once
        # per pair.
        app.wait_for("#mergeask-options .mergeask-opt input")
        app.click("#mergeask-merge")

        app.tab.wait_for(
            f"document.querySelectorAll('#peoplegrid .pcard').length === {before - 1}",
            timeout=15.0,
            what="the two chosen groups to become one",
        )
        # The mode ends with the act: what was chosen no longer exists.
        assert app.count("#selectbar") == 0
        assert app.count("#peoplegrid.selecting") == 0
        assert app.errors() == []


def test_leaving_the_screen_ends_the_selection(open_app):
    """A bar left over the next screen would offer to merge groups nobody is
    looking at any more."""
    with open_app("people") as app:
        app.wait_for("#peoplegrid .pcard")
        _select_cards(app, 1)
        assert app.count("#selectbar") == 1

        app.show_section("overview")

        assert app.count("#selectbar") == 0
        assert app.errors() == []
