"""A document in the viewer: what the panel says about it, where the stage puts
it, and who has the keyboard while it is open.

Split from test_viewer.py, which is about the viewer as a viewer -- the arrows,
the inspector, the zoom. These are about the one kind of file the viewer does
not draw itself: a PDF, an Office file, a scan, handed to the browser's own
viewer inside an iframe. Everything peculiar here follows from that frame. Its
toolbar is why the stage starts below our chrome, and its keyboard handling is
why the app has a reading mode at all.
"""


def _open_document(app, archive, key="document"):
    """Open one file straight by id and wait for the panel to name it."""
    app.tab.evaluate(f"openItem({archive.ids[key]})")
    app.tab.wait_for(
        "!!(document.querySelector('#modal.open #minfo h3') || {}).textContent",
        what="the panel to name the file it opened",
    )


def test_a_document_reports_its_reader_and_never_its_text(open_app, archive):
    """A picture's transcript belongs in the panel; a document's words do not --
    the document itself is what the stage puts on screen."""
    with open_app("library", wait_for=".tile") as app:
        _open_document(app, archive)
        panel = app.text("#minfo")

        assert "Detected text" in panel
        assert "text layer" in panel  # which reader found it
        assert "The lease agreement" not in panel  # never the words
        assert app.errors() == []


def test_a_picture_reports_the_whole_transcript(open_app, archive):
    with open_app("library", wait_for=".tile") as app:
        _open_document(app, archive, "ocr_photo")

        assert "RECIBO DE COMPRA" in app.text("#minfo .textcard")
        assert app.errors() == []


def test_a_document_is_put_on_the_stage_clear_of_the_chrome(open_app, archive):
    """The browser's own PDF viewer draws its toolbar -- page number, zoom --
    along the top of the frame, so the stage has to start below our floating
    chrome or the two sit on top of each other."""
    with open_app("library", wait_for=".tile") as app:
        _open_document(app, archive)
        app.wait_for(".docstage")

        box = app.tab.evaluate(
            "(r => ({top: Math.round(r.top), height: Math.round(r.height),"
            " bottom: Math.round(r.bottom)}))"
            "(document.querySelector('.docstage').getBoundingClientRect())"
        )
        assert box["top"] >= 50, "the document stage runs under the floating chrome"
        # An <iframe> brings presentational width/height attributes that beat the
        # top/bottom stretch, which left the PDF in a 300x150 box in the corner.
        assert box["height"] > 400, f"the document stage did not fill the frame: {box}"
        # ...and it ends where the inspector beside it ends.
        assert box["bottom"] == app.tab.evaluate(
            "Math.round(document.getElementById('minfo').getBoundingClientRect().bottom)"
        )
        assert app.errors() == []


# Reading mode as the browser really delivers it: focus lands in the frame and
# the window loses it, which is the only signal this page gets -- an iframe's
# focus does not bubble and the document inside it is the browser's own.
_INTO_DOCUMENT = """(() => {
  document.querySelector('.docstage iframe').focus();
  window.dispatchEvent(new Event('blur'));
})()"""

_BACK_TO_THE_PAGE = """(() => {
  document.getElementById('viewer').focus();
  window.dispatchEvent(new Event('focus'));
})()"""


def test_clicking_back_onto_the_page_takes_the_keyboard_back(open_app, archive):
    """Clicking into an embedded PDF hands the arrows to the browser's own
    viewer, and the pill says so. Getting them back was advertised as Esc --
    a key that is delivered to the frame and never leaves it, so the one way
    out the viewer named was the one it could not receive.

    What the page can see is focus coming home, which is the same gesture
    anyone would make anyway. So that is what ends it.
    """
    with open_app("library", wait_for=".tile") as app:
        _open_document(app, archive)
        app.wait_for(".docstage iframe")

        app.tab.evaluate(_INTO_DOCUMENT)
        app.wait_for(".vpos.reading")
        assert "arrows belong to the document" in app.text(".vpos.reading")

        app.tab.evaluate(_BACK_TO_THE_PAGE)

        assert app.count(".vpos.reading") == 0, "the page took the keyboard back and said nothing"
        assert app.errors() == []


def test_the_pill_is_itself_the_way_out_of_a_document(open_app, archive):
    """For anyone who reads the notice rather than clicking past it: the
    sentence describing the state is the control that ends it."""
    with open_app("library", wait_for=".tile") as app:
        _open_document(app, archive)
        app.wait_for(".docstage iframe")
        app.tab.evaluate(_INTO_DOCUMENT)
        app.wait_for(".vpos-out")

        app.click(".vpos-out")

        assert app.count(".vpos.reading") == 0
        # ...and the viewer is still open, because stepping out of a document
        # is not leaving the file.
        assert app.count("#viewer .docstage") == 1
        assert app.errors() == []


def test_the_close_button_closes_even_after_a_document_had_the_keyboard(open_app, archive):
    """It is labelled "Close (Esc)". It used to step out of reading mode
    instead and leave the viewer standing, which made the one button on the
    screen that promises to close the one that would not."""
    with open_app("library", wait_for=".tile") as app:
        _open_document(app, archive)
        app.wait_for(".docstage iframe")
        app.tab.evaluate(_INTO_DOCUMENT)
        app.wait_for(".vpos.reading")

        # Pressing it is a click on the page, so focus comes home with it.
        app.tab.evaluate(_BACK_TO_THE_PAGE)
        app.tab.evaluate("closeModal()")

        assert app.count("#modal.open") == 0, "the close button did not close"
        assert app.errors() == []
