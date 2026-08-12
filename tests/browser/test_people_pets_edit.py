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


def test_a_persons_recent_changes_open_from_the_top_bar(open_app):
    """The merge list used to sit permanently between the name and the photos.

    Asserting it is *absent* until asked for is half the point of the change,
    so the first assertion is that the page opens on the faces.
    """
    with open_app("people") as app:
        app.wait_for_text("Ada")
        app.click(".pcard img.face, .pcard .facecollage img")
        app.wait_for("#histmenu")
        assert app.count(".hist-menu .hist-row") == 0, "the history should not be open on arrival"

        app.click("#histmenu > summary")
        app.wait_for(".hist-row, .hist-empty")
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
