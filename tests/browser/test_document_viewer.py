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


# Focus inside the frame, as the browser really delivers it: the window loses
# focus and nothing else is reported, since an iframe's focus does not bubble
# and the document inside it is the browser's own.
_INTO_DOCUMENT = """(() => {
  document.querySelector('.docstage iframe').focus();
  window.dispatchEvent(new Event('blur'));
})()"""


def test_a_document_holding_the_keyboard_puts_nothing_on_the_screen(open_app, archive):
    """Clicking into an embedded PDF hands the arrows to the browser's own
    viewer. There is nothing this app can do about that -- the keys go to a
    document in an iframe that is not ours -- and it briefly announced the fact
    with a pill over the stage, which was the only mode the app had, could not
    deliver the Esc it advertised, and left the Close button not closing.

    Explaining a browser behaviour is not worth a mode. The chrome says the
    same thing it says for any other file, and clicking back on the page gives
    the keys back without anything having to be dismissed.
    """
    with open_app("library", wait_for=".tile") as app:
        _open_document(app, archive)
        app.wait_for(".docstage iframe")
        readout = app.text(".vpos")

        app.tab.evaluate(_INTO_DOCUMENT)

        assert app.count(".vpos.reading") == 0
        assert app.count(".vpos-out") == 0
        assert app.text(".vpos") == readout, "the chrome changed under the document"
        assert app.errors() == []


def test_the_close_button_closes_a_document(open_app, archive):
    """It is labelled "Close (Esc)". While there was a reading mode it stepped
    out of that first and left the viewer standing, which made the one button
    on the screen promising to close the one that would not."""
    with open_app("library", wait_for=".tile") as app:
        _open_document(app, archive)
        app.wait_for(".docstage iframe")
        app.tab.evaluate(_INTO_DOCUMENT)

        app.tab.evaluate("closeModal()")

        assert app.count("#modal.open") == 0, "the close button did not close"
        assert app.errors() == []
