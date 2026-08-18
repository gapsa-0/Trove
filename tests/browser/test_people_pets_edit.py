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


def test_a_person_is_still_renamed_from_their_card(open_app):
    """The people path shares nameedit.js now; it must not have regressed."""
    with open_app("people") as app:
        app.wait_for_text("Ada")
        app.click(".pcard .pname")
        app.wait_for(".pcard .pmeta-editing input")
        _type_into(app, ".pcard .pmeta-editing input", "Ada L")
        app.wait_for_text("Ada L")
        assert app.errors() == []


def test_a_group_counts_itself_in_files_not_photographs(open_app):
    """A face is found in a video as readily as in a photograph.

    The count under a name is a count of distinct files, so a group holding a
    clip called itself "11 photos" and was wrong about two things at once --
    what it holds, and what the screen it opens can show.
    """
    for section in ("people", "pets"):
        with open_app(section) as app:
            app.wait_for(".pcard .pcount")
            counts = app.tab.evaluate(
                "[...document.querySelectorAll('.pcard .pcount')].map(e => e.textContent)"
            )
            assert counts, f"no counts on the {section} cards to read"
            for text in counts:
                assert "photo" not in text, f"{section} card still counts photographs: {text!r}"
                assert "file" in text, f"{section} card counts nothing recognisable: {text!r}"


def test_a_person_with_a_quotation_mark_in_their_name_can_be_renamed(open_app):
    """The page's rename button carried the name inside a click attribute.

    It escaped a backslash and an apostrophe for the JavaScript string, and
    nothing for the HTML attribute around it, so a quotation mark ended the
    attribute early: the button rendered, looked exactly right, and did
    nothing. Named through the app's own editor rather than through the API,
    so what is under test is the page drawn from a name the app itself stored.
    """
    with open_app("people") as app:
        app.wait_for_text("Ada")
        app.click(".pcard .pname")
        app.wait_for(".pcard .pmeta-editing input")
        _type_into(app, ".pcard .pmeta-editing input", 'Ana "Nana"')
        app.wait_for_text('Ana "Nana"')

        app.click(".pcard img.face, .pcard .facecollage img")
        app.wait_for("#personname .person-name-button")
        app.click("#personname .person-name-button")
        app.wait_for("#personname input")
        assert app.errors() == []


def test_a_face_nobody_has_named_can_be_named_from_the_photo(open_app, archive):
    """The panel could only ever point a face at somebody already named.

    On an archive where nobody is named that left it with nothing to offer but
    a sentence sending you to another screen -- and a face belonging to no group
    at all, which is most of what detection finds, had no way to be named from
    anywhere. This is the whole round trip: the control, the field, the save,
    and the panel coming back with the name on it.
    """
    with open_app("library", wait_for=".tile") as app:
        app.tab.evaluate(f"openItem({archive.ids['file_unnamed_face']})")
        app.wait_for("#minfo .facerow .facename")

        app.click("#minfo .facerow .facename")
        app.wait_for("#minfo .facerow .inline-name-editor input")
        app.tab.evaluate(
            "(() => { const i = document.querySelector('#minfo .inline-name-editor input');"
            " i.value = 'Bruno'; i.dispatchEvent(new Event('blur')); })()"
        )

        app.tab.wait_for(
            "document.getElementById('minfo').textContent.includes('Bruno')",
            timeout=10.0,
            what="the name to come back on the panel",
        )
        assert app.errors() == []
