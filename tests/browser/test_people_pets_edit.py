"""Renaming a person or a pet, driven through the real controls.

The tier matters here. Renaming is wired across four layers, and every layer
but this one was already green while the feature was dead in the user's hands:
the endpoint has an API test, the handler names have a static check, and
neither can see that a pet's grid name carried no click handler at all, or that
its page called ``window.prompt`` -- which the desktop shell defines as a
function that throws.

So these assert the control, not the request. And they assert that an *input
appeared*, never merely that nothing threw: headless Chrome implements
``prompt`` and simply returns null, so the regression these guard against is
invisible to an error check in this browser and only bites in Electron.
"""

from __future__ import annotations


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


def test_a_pet_is_renamed_from_its_card_in_the_grid(open_app):
    """The grid's name was a <div> with no handler, styled to look editable."""
    with open_app("pets") as app:
        app.wait_for_text("Kira")
        app.click(".pcard .pname")
        # The control exists and opens an editor -- the whole assertion.
        app.wait_for(".pcard .pmeta-editing input")
        _type_into(app, ".pcard .pmeta-editing input", "Kirita")
        app.wait_for_text("Kirita")
        assert app.errors() == []


def test_a_pet_is_renamed_from_its_own_page(open_app):
    """This is the path that called prompt() and threw in the desktop build."""
    with open_app("pets") as app:
        app.wait_for_text("Kira")
        app.click(".pcard img.face")
        app.wait_for("#petname .person-name-button")
        app.click("#petname .person-name-button")
        app.wait_for("#petname input")
        _type_into(app, "#petname input", "Kirita")
        app.wait_for_text("Kirita")
        assert app.errors() == []


def test_an_edit_patches_the_people_grid_instead_of_rebuilding_it(open_app):
    """Node identity is the assertion, not the card count.

    A rebuild produces a grid that *looks* right -- same names, same order --
    so counting cards passes either way. Marking the cards and checking an
    untouched one is still the same DOM node afterwards is what distinguishes
    a patch from a teardown, and the teardown is what threw away the scroll
    position and every loaded page on a screen with hundreds of clusters.

    Driven through a rename because that is a real click; it runs
    ``refreshPeopleGrid``, which is the same function ``attachMergeDrag`` and
    the "Same person?" queue hand their merges to.
    """
    with open_app("people") as app:
        app.wait_for_text("Ada")
        app.wait_for(".pcard")
        assert app.count(".pcard") >= 2, "this asserts about a card other than the edited one"
        app.tab.evaluate(
            "[...document.querySelectorAll('.pcard')].forEach((c, i) => { c.__mark = i; })"
        )
        # Rename the first card; the second is the one under test.
        app.tab.evaluate("document.querySelector('.pcard .pname').click()")
        app.wait_for(".pcard .pmeta-editing input")
        _type_into(app, ".pcard .pmeta-editing input", "Ada Lovelace")
        app.wait_for_text("Ada Lovelace")

        survivors = app.tab.evaluate(
            "[...document.querySelectorAll('.pcard')].filter(c => c.__mark !== undefined).length"
        )
        assert survivors >= 1, "every card was replaced -- the grid was rebuilt, not patched"
        assert app.errors() == []


def test_a_named_person_can_be_made_unnamed_again(open_app):
    """Emptying the field always did this; nothing said so."""
    with open_app("people") as app:
        app.wait_for_text("Ada")
        app.click(".pcard .pname")
        app.wait_for(".pcard .pmeta-editing .name-clear")
        app.tab.evaluate(
            "document.querySelector('.pcard .pmeta-editing .name-clear')"
            ".dispatchEvent(new Event('pointerdown', {bubbles: true}))"
        )
        app.wait_for_text("Name this person")
        assert app.errors() == []


def test_an_unnamed_person_is_not_offered_a_name_to_remove(open_app):
    """The control is about undoing a name, so it belongs only where there is
    one; on an unnamed cluster it would be a button that does nothing."""
    with open_app("people") as app:
        app.wait_for_text("Ada")
        unnamed = app.tab.evaluate(
            "(() => { const c = [...document.querySelectorAll('.pcard')]"
            ".find(c => c.querySelector('.pname.un')); if (!c) return false;"
            " c.querySelector('.pname').click(); return true; })()"
        )
        if not unnamed:
            return  # the seed has no unnamed cluster; nothing to assert
        app.wait_for(".pcard .pmeta-editing input")
        assert app.count(".pcard .pmeta-editing .name-clear") == 0


def test_hiding_a_person_as_unknown_moves_them_into_the_hidden_section(open_app):
    """The whole round trip, through the controls, in one test: the menu opens,
    the card leaves the grid, the Hidden section appears with it inside, and
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
        assert "Hidden" in app.text("#hiddenwrap")

        app.tab.evaluate("document.querySelector('.hidden-people').setAttribute('open', '')")
        app.wait_for(".hidden-people .pcard.is-hidden")
        app.click(".hidden-people .pcard.is-hidden .linkbtn")
        app.tab.wait_for(
            f"document.querySelectorAll('#peoplegrid .pcard').length === {before}",
            timeout=10.0,
            what="the restored card to come back to the grid",
        )
        assert app.errors() == []


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


def test_a_person_is_still_renamed_from_their_card(open_app):
    """The people path shares nameedit.js now; it must not have regressed."""
    with open_app("people") as app:
        app.wait_for_text("Ada")
        app.click(".pcard .pname")
        app.wait_for(".pcard .pmeta-editing input")
        _type_into(app, ".pcard .pmeta-editing input", "Ada L")
        app.wait_for_text("Ada L")
        assert app.errors() == []
