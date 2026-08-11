"""The media viewer: does it open, does it move, and does it keep quiet about
work this archive never asked for.

The tier's usual reason applies twice over here. The viewer is the one screen
assembled from three modules at once -- item.js drives it, panel.js renders what
it says, gallery.js decides what its arrows walk -- and a name that moved
between them fails at *module load*, which takes the whole app down with no
error any Python test or eslint run can see. That happened once while this
viewer was being built.

`_configure` mirrors the helper in test_setup_screen.py: the archive fixture
turns every feature on, and half of what is checked here is what a viewer shows
when a feature is off.
"""

import json
import urllib.request

import pytest


def _configure(archive, **body):
    """Change the archive's setup through the API the screen itself posts to.

    Same helper as test_setup_screen.py: the server holds the archive open, so
    editing the registry underneath it is not the same thing as the app
    reconfiguring itself.
    """
    request = urllib.request.Request(
        f"{archive.base_url}/api/archive/configure",
        data=json.dumps({"root_id": archive.root_id, **body}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def _open_first_photo(app):
    """Open the first tile in Browse and wait for the panel to be drawn."""
    app.tab.evaluate("document.querySelector('#grid .tile').click()")
    app.tab.wait_for(
        "!!(document.querySelector('#modal.open #minfo h3') || {}).textContent",
        what="the panel to name the file it opened",
    )


def test_opening_an_item_draws_the_stage_and_the_panel(open_app, archive):
    with open_app("library", wait_for=".tile") as app:
        _open_first_photo(app)

        assert app.count("#mmedia img") == 1
        assert app.count("#minfo .isec") > 0
        assert app.errors() == []


def test_the_arrow_keys_move_to_the_next_file(open_app, archive):
    """The headline of the whole redesign, and the thing that silently did
    nothing on five of six screens before it."""
    with open_app("library", wait_for=".tile") as app:
        _open_first_photo(app)
        first = app.tab.evaluate("document.querySelector('#minfo h3').textContent")

        app.tab.evaluate(
            "document.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}))"
        )
        app.tab.wait_for(
            f"document.querySelector('#minfo h3').textContent !== {first!r}",
            what="the viewer to move to the next file",
        )

        # ...and back again, to the file it started on.
        app.tab.evaluate(
            "document.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowLeft',bubbles:true}))"
        )
        app.tab.wait_for(
            f"document.querySelector('#minfo h3').textContent === {first!r}",
            what="the viewer to move back",
        )
        assert app.errors() == []


def test_the_position_readout_names_the_set_the_arrows_walk(open_app, archive):
    with open_app("library", wait_for=".tile") as app:
        _open_first_photo(app)

        assert "in Browse" in app.tab.evaluate("document.getElementById('vpos').textContent")
        assert app.errors() == []


def test_a_file_opened_on_its_own_gets_no_arrows(open_app, archive):
    """A search that matched one file, a place holding one photo, a file opened
    from somewhere with no set behind it. The arrows used to stay, greyed out,
    which is the right answer at the *end* of a set -- it says there are others
    and which way they lie -- and no answer at all when there is no set. Two
    dead controls over the picture instead.

    Driven by opening a file with a gallery of one, which is what all three of
    those cases produce.
    """
    with open_app("library", wait_for=".tile") as app:
        _open_first_photo(app)
        assert app.count("#vprev:not([hidden])") == 1, "a real set lost its arrows"

        one = archive.ids["first_file"]
        app.tab.evaluate(
            f"import('/static/js/gallery.js').then(m => {{"
            f" m.setGallery([{one}], 'in this search'); openItem({one}); }})"
        )
        app.tab.wait_for(
            "document.getElementById('vprev').hidden",
            what="the arrows to go with the set they walked",
        )

        assert app.count("#vnext[hidden]") == 1
        # The filmstrip already answered this question the same way, and the
        # readout names the file rather than inventing "1 of 1".
        assert app.count(".filmstrip[hidden]") == 1
        assert "of" not in app.text("#vpos")
        assert app.errors() == []


def test_the_arrows_walk_a_places_photos_when_opened_from_a_place(open_app, archive):
    """`S.gallery` used to be filled in exactly one place -- Browse -- so on
    every other screen the arrows silently did nothing.

    Places rather than People because People needs OpenCV's DNN face module to
    draw a card at all, and this tier has to run on a machine without it.
    """
    with open_app("places", wait_for=".pcard") as app:
        app.tab.evaluate("document.querySelector('.pcard').click()")
        app.wait_for("#mapsidegrid .tile")
        app.tab.evaluate("document.querySelector('#mapsidegrid .tile').click()")
        app.tab.wait_for(
            "!!(document.querySelector('#modal.open #minfo h3') || {}).textContent",
            what="the panel to name the file it opened",
        )

        # Bounded by the place, and the readout says so rather than implying
        # the arrows are about to wander into the rest of the archive.
        assert "at this place" in app.tab.evaluate("document.getElementById('vpos').textContent")
        assert app.errors() == []


def test_the_inspector_can_be_dismissed_and_brought_back(open_app, archive):
    with open_app("library", wait_for=".tile") as app:
        _open_first_photo(app)
        assert app.count("#viewer.rail-on") == 1

        app.tab.evaluate(
            "document.dispatchEvent(new KeyboardEvent('keydown',{key:'i',bubbles:true}))"
        )
        app.tab.wait_for(
            "document.querySelectorAll('#viewer.rail-on').length === 0",
            what="the inspector to close",
        )

        app.tab.evaluate(
            "document.dispatchEvent(new KeyboardEvent('keydown',{key:'i',bubbles:true}))"
        )
        app.tab.wait_for(
            "document.querySelectorAll('#viewer.rail-on').length === 1",
            what="the inspector to come back",
        )
        assert app.errors() == []


def test_a_declined_feature_leaves_no_section_behind(open_app, archive):
    """Not an empty section, not an explanation -- nothing. An archive of
    scanned paperwork should never be told 'no faces here'."""
    _configure(archive, features=["index", "duplicates"])

    with open_app("library", wait_for=".tile") as app:
        _open_first_photo(app)
        panel = app.text("#minfo")

        assert "People" not in panel
        assert "Pets" not in panel
        assert "faces" not in panel.lower()
        # ...while the sections that do not depend on a feature are still here.
        assert "Details" in panel
        assert "File" in panel
        assert app.errors() == []


def test_a_file_no_stage_has_reached_says_so_rather_than_claiming_nothing(open_app, archive):
    """The distinction the panel exists to make: the seeded photos have no
    face_scan row, so nothing has looked at them yet."""
    with open_app("library", wait_for=".tile") as app:
        _open_first_photo(app)
        panel = app.text("#minfo")

        assert "not read yet" in panel.lower()
        # The wording for the other state must not appear for an unread file.
        assert "no faces here" not in panel.lower()
        assert app.errors() == []


def test_closing_the_viewer_leaves_nothing_behind(open_app, archive):
    """A stage left holding a <video> or an <iframe> goes on playing, and keeps
    the keyboard, behind a closed viewer."""
    with open_app("library", wait_for=".tile") as app:
        _open_first_photo(app)
        app.tab.evaluate(
            "document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))"
        )
        app.tab.wait_for(
            "document.querySelectorAll('#modal.open').length === 0",
            what="the viewer to close",
        )

        assert app.count("#mmedia img") == 0
        assert app.count("#viewer .docstage") == 0
        assert app.errors() == []


def test_the_media_and_the_panel_agree_where_the_viewer_ends(open_app, archive):
    """They used to disagree: the stage filled the frame and ran on behind the
    filmstrip while the inspector beside it stopped above the strip."""
    with open_app("library", wait_for=".tile") as app:
        _open_first_photo(app)

        stage = app.tab.evaluate(
            "Math.round(document.getElementById('mmedia').getBoundingClientRect().bottom)"
        )
        panel = app.tab.evaluate(
            "Math.round(document.getElementById('minfo').getBoundingClientRect().bottom)"
        )
        strip = app.tab.evaluate(
            "Math.round(document.querySelector('.filmstrip').getBoundingClientRect().top)"
        )

        assert stage == panel, "the media and the inspector end at different heights"
        assert stage <= strip, "the media runs on behind the filmstrip"
        assert app.errors() == []


def test_the_face_box_control_lives_with_the_people_it_shows(open_app, archive):
    """Showing where the faces are is a fact about the People section, so the
    control for it is there rather than in the chrome over the photograph."""
    with open_app("library", wait_for=".tile") as app:
        app.tab.evaluate(f"openItem({archive.ids['first_file']})")
        app.tab.wait_for(
            "!!document.getElementById('boxtoggle')",
            what="the face-box toggle in the People section",
        )

        assert app.tab.evaluate(
            "document.getElementById('boxtoggle').closest('.isec').textContent.includes('People')"
        )
        assert app.errors() == []


def test_no_face_box_control_on_an_archive_without_people(open_app, archive):
    """With the feature off there is no People section at all, so there is
    nothing to carry the control either."""
    _configure(archive, features=["index", "duplicates"])

    with open_app("library", wait_for=".tile") as app:
        _open_first_photo(app)

        assert app.count("#boxtoggle") == 0
        assert app.errors() == []


def test_hovering_a_face_row_lights_up_that_persons_box(open_app, archive):
    """The only way to tell which box is whose in a group shot."""
    with open_app("library", wait_for=".tile") as app:
        app.tab.evaluate(f"openItem({archive.ids['first_file']})")
        app.wait_for(".facerow[data-face-id]")
        # A box can only be placed against a decoded image, so its position is
        # known; until then drawBoxes defers to the load event.
        #
        # Generously timed, and not because the feature is slow: opening Browse
        # queues 120 thumbnails, each generated on demand into a cold cache, and
        # this full-size request waits its turn behind them on one of Chrome's
        # six connections. The default 10s is a fixture backlog away from
        # failing, which is a flake rather than a finding.
        app.tab.wait_for(
            "(i => !!i && i.complete && i.naturalWidth > 0)(document.querySelector('#mmedia img'))",
            timeout=30.0,
            what="the photograph to decode",
        )

        assert app.count(".facebox.hot") == 0
        face = app.tab.evaluate("document.querySelector('.facerow[data-face-id]').dataset.faceId")
        app.tab.evaluate(f"highlightFace({face}); 1")

        app.tab.wait_for(
            "document.querySelectorAll('.facebox.hot').length === 1",
            what="the hovered face's box to light up",
        )
        app.tab.evaluate("highlightFace(null); 1")
        assert app.count(".facebox.hot") == 0
        assert app.errors() == []


def _panel_overflows(app):
    """True when anything in the panel is wider than the panel itself."""
    return app.tab.evaluate(
        "(p => p.scrollWidth > p.clientWidth + 1)(document.getElementById('minfo'))"
    )


def test_the_date_its_source_and_its_edit_control_share_one_line(open_app, archive):
    """One fact and its provenance. The row used to break them across three
    places: "Edit" up in the label column beside the word "Date", the date on
    the right, and where it came from dropping to a second line underneath --
    two lines and a filled chip for six words.

    Asserted as the height of the row, because that is the thing that was
    wrong: any wording that fits stays one line, and any that does not is back
    where this started.
    """
    with open_app("library", wait_for=".tile") as app:
        _open_first_photo(app)

        row = app.tab.evaluate(
            "(r => Math.round(r.height))(document.querySelector('.datekv').getBoundingClientRect())"
        )
        line = app.text(".datekv")

        assert row < 40, f"the date row is {row}px, so something wrapped"
        assert "From metadata" in line, line
        # The way in is a control at the end of the row, not a word in front of
        # the label -- and it still opens the same editor.
        assert app.count(".datekv .k .linkbtn") == 0
        app.click(".datekv .iconbtn")
        app.wait_for("#dateval .dtrow")
        assert app.errors() == []


def test_the_date_editor_stays_inside_the_panel(open_app, archive):
    """The editor is three fields and two buttons. Sized to its content inside
    a right-aligned half-row it pushed a horizontal scrollbar onto the whole
    panel."""
    with open_app("library", wait_for=".tile") as app:
        _open_first_photo(app)
        assert not _panel_overflows(app)

        app.tab.evaluate("editDate()")
        app.wait_for("#dateval .dtrow")

        assert not _panel_overflows(app), "the date editor is wider than the panel"
        # ...and the fields are actually usable, not squeezed to nothing.
        assert app.tab.evaluate("document.getElementById('d-y').getBoundingClientRect().width") > 40
        assert app.errors() == []


def test_related_pictures_are_a_grid_rather_than_a_sideways_scroll(open_app, archive):
    """Every result on screen at once: a panel that already scrolls downward
    should not hide half an answer off its right edge."""
    with open_app("library", wait_for=".tile") as app:
        _open_first_photo(app)
        # Draw the strip directly: the fixture has no embeddings, and what is
        # being checked is the layout the results land in.
        app.tab.evaluate(
            "document.getElementById('minfo').insertAdjacentHTML('beforeend',"
            "`<div class='relstrip'>${'<button><span></span></button>'.repeat(8)}</div>`)"
        )

        strip = app.tab.evaluate("getComputedStyle(document.querySelector('.relstrip')).display")
        assert strip == "grid"
        # Eight results on more than one row, and none of them off the edge.
        tops = app.tab.evaluate(
            "[...document.querySelectorAll('.relstrip button')]"
            ".map(b => Math.round(b.getBoundingClientRect().top))"
        )
        assert len(set(tops)) > 1, "the results are still on one row"
        assert not _panel_overflows(app)
        assert app.errors() == []


def _open(app, file_id):
    """Open one file by id and wait for its panel."""
    app.tab.evaluate(f"openItem({file_id})")
    app.tab.wait_for(
        "!!(document.querySelector('#modal.open #minfo h3') || {}).textContent",
        what="the panel to name the file it opened",
    )


def test_the_copies_of_a_file_are_shown_rather_than_counted(open_app, archive):
    """ "3 copies" says three files somewhere are the same and leaves you to go
    and find them. The group is small and already grouped, so it is drawn."""
    with open_app("library", wait_for=".tile") as app:
        _open(app, archive.ids["dup_kept"])
        app.wait_for("#minfo .copies")

        assert app.count("#minfo .copy") == 2
        # Where you are among them, and which one Trove keeps -- the file you
        # opened is both here, and does not offer to open itself.
        assert app.count("#minfo .copy.here") == 1
        assert app.count("#minfo button.copy.here") == 0, "the open file offers to open itself"
        section = app.tab.evaluate(
            "document.querySelector('#minfo .copies').closest('.isec').textContent"
        )
        assert "Duplicates" in section
        # The Duplicates screen's own words for the same fact, not a second set
        # of them: this used to read "Looks the same" here and "Visual match"
        # there, which is how one fact becomes two in a reader's head -- and it
        # sat directly under a section called "Looks like this", which means
        # something else again.
        assert "This file" in section and "Visual match" in section
        assert "1 other copy" in section
        assert app.errors() == []


def test_opening_a_copy_from_the_panel_bounds_the_arrows_to_the_group(open_app, archive):
    """Same claim about "next" the Duplicates screen makes about its tiles: the
    group is the set you are comparing, and running off the end of it lands you
    on an unrelated photograph. Back returns to the copy you came from."""
    with open_app("library", wait_for=".tile") as app:
        _open(app, archive.ids["dup_kept"])
        app.wait_for("#minfo .copies")
        started_on = app.tab.evaluate("document.querySelector('#minfo h3').textContent")

        app.tab.evaluate("document.querySelector('#minfo button.copy').click()")
        app.tab.wait_for(
            f"document.querySelector('#minfo h3').textContent !== {started_on!r}",
            what="the viewer to land on the other copy",
        )

        assert "in this duplicate group" in app.text("#vpos")
        app.tab.evaluate("viewerBack(); 1")
        app.tab.wait_for(
            f"document.querySelector('#minfo h3').textContent === {started_on!r}",
            what="the viewer to return to the copy the jump started from",
        )
        assert "in Browse" in app.text("#vpos")
        assert app.errors() == []


def test_a_compared_file_with_no_copies_says_so(open_app, archive):
    """And says it as a finding, not as the pulse that means "still queued":
    this file has been through a grouping run and nothing matched it."""
    with open_app("library", wait_for=".tile") as app:
        _open(app, archive.ids["ocr_photo"])
        panel = app.text("#minfo")

        assert "No duplicates found" in panel
        assert app.count("#minfo .copies") == 0
        assert app.errors() == []


def _zoom(app):
    return app.tab.evaluate(
        "Math.round(parseFloat("
        "(document.querySelector('#mmedia img').style.transform.match(/scale\\(([\\d.]+)/) || [0, 1])[1]"
        ") * 100)"
    )


def test_the_wheel_zooms_the_photo_and_the_bar_follows(open_app, archive):
    with open_app("library", wait_for=".tile") as app:
        _open_first_photo(app)
        assert _zoom(app) == 100
        # Nothing to control at fit, so no bar over the photograph.
        assert app.tab.evaluate("document.getElementById('zoombar').offsetParent === null")

        app.tab.evaluate(
            "document.getElementById('mmedia').dispatchEvent(new WheelEvent('wheel',"
            "{deltaY:-400, clientX:700, clientY:450, bubbles:true, cancelable:true})); 1"
        )

        assert _zoom(app) > 100, "the wheel did not zoom the photo"
        # ...and now the control appears, because there is something to control.
        assert app.tab.evaluate("document.getElementById('zoombar').offsetParent !== null")
        # The readout and the slider are driven from the same number.
        assert (
            app.tab.evaluate("document.getElementById('zoom-pct').textContent") == f"{_zoom(app)}%"
        )
        assert app.tab.evaluate("Number(document.getElementById('zoom-range').value)") > 0
        assert app.errors() == []


def test_zoom_resets_between_files(open_app, archive):
    """Carrying 400% onto the next file turns arrowing through an archive into a
    sequence of arbitrary crops."""
    with open_app("library", wait_for=".tile") as app:
        _open_first_photo(app)
        app.tab.evaluate("zoomStep(1); zoomStep(1); 1")
        assert _zoom(app) > 100

        first = app.tab.evaluate("document.querySelector('#minfo h3').textContent")
        app.tab.evaluate(
            "document.dispatchEvent(new KeyboardEvent('keydown',"
            "{key:'ArrowRight',bubbles:true})); 1"
        )
        # On the NEXT file, not merely on the outgoing one's image: openItem is
        # async, and the old <img> is still on the stage until it resolves.
        app.tab.wait_for(
            f"document.querySelector('#minfo h3').textContent !== {first!r}"
            " && !!document.querySelector('#mmedia img')",
            what="the viewer to land on the next file",
        )

        assert _zoom(app) == 100, "the zoom followed the viewer onto the next file"
        assert app.errors() == []


def test_the_zoom_control_is_only_there_for_a_still_image(open_app, archive):
    with open_app("library", wait_for=".tile") as app:
        app.tab.evaluate(f"openItem({archive.ids['document']})")
        app.wait_for(".docstage")

        assert app.tab.evaluate("document.getElementById('zoombar').offsetParent === null"), (
            "a document is offering a zoom control"
        )
        assert app.errors() == []


def test_fit_returns_a_zoomed_photo_to_the_frame(open_app, archive):
    with open_app("library", wait_for=".tile") as app:
        _open_first_photo(app)
        app.tab.evaluate("zoomStep(1); 1")
        assert _zoom(app) > 100

        app.tab.evaluate("zoomReset(); 1")

        assert _zoom(app) == 100
        # ...and the pan goes with it, so Fit is genuinely the opening view.
        assert app.tab.evaluate("document.querySelector('#mmedia img').style.transform").startswith(
            "translate(0px, 0px)"
        )
        assert app.errors() == []


def test_a_jump_into_similar_pictures_can_be_undone(open_app, archive):
    """Opening a picture out of "Looks like this" used to be a dead end: the
    gallery still held the screen you came from, so that file was in no gallery
    at all -- arrows off, filmstrip gone, and nothing behind the viewer to go
    back to."""
    with open_app("library", wait_for=".tile") as app:
        _open_first_photo(app)
        started_on = app.tab.evaluate("document.querySelector('#minfo h3').textContent")
        assert "in Browse" in app.tab.evaluate("document.getElementById('vpos').textContent")
        assert app.tab.evaluate("document.getElementById('vback').offsetParent === null")

        app.tab.evaluate(f"openRelated({archive.ids['ocr_photo']}); 1")
        app.tab.wait_for(
            f"document.querySelector('#minfo h3').textContent !== {started_on!r}",
            what="the viewer to land on the jumped-to picture",
        )

        # There is now a way back, and it is the only way back.
        assert app.tab.evaluate("document.getElementById('vback').offsetParent !== null")

        app.tab.evaluate("viewerBack(); 1")
        app.tab.wait_for(
            f"document.querySelector('#minfo h3').textContent === {started_on!r}",
            what="the viewer to return to where the jump started",
        )

        # ...and the gallery it was walking came back with it.
        assert "in Browse" in app.tab.evaluate("document.getElementById('vpos').textContent")
        assert app.tab.evaluate("document.getElementById('vback').offsetParent === null")
        assert app.errors() == []


def test_an_ordinary_open_clears_the_way_back(open_app, archive):
    """Back means "undo that jump", not "the last file I looked at" -- arrowing
    on from a jumped-to picture is navigating, not jumping."""
    with open_app("library", wait_for=".tile") as app:
        _open_first_photo(app)
        app.tab.evaluate(f"openRelated({archive.ids['ocr_photo']}); 1")
        app.tab.wait_for("document.getElementById('vback').offsetParent !== null")

        app.tab.evaluate(f"openItem({archive.ids['document']}); 1")
        app.tab.wait_for("document.getElementById('vback').offsetParent === null")

        assert app.errors() == []


def _item(archive, fid):
    """One file's payload, straight from the API the viewer reads it from."""
    with urllib.request.urlopen(f"{archive.base_url}/api/item/{fid}?root={archive.root_id}") as r:
        return json.load(r)


def test_a_video_the_window_refuses_says_so_instead_of_going_black(open_app, archive):
    """The stage used to hand the file to a <video> and leave it there.

    A format the window has no reader for -- .avi and .wmv, most of an old
    camcorder shelf -- then sat as a black rectangle with a dead transport
    under it and nothing to say why, which reads as Trove having broken rather
    than as the player having limits.

    Which of the two messages it ends on depends on the machine, so the machine
    is asked rather than assumed: with an ffmpeg to re-encode through, this file
    has been through it and failed there too, and the honest answer is that
    nothing here could read it. Without one, the format is still the story.
    Pinning either wording outright would fail on half the machines that run
    this.
    """
    with open_app("library", wait_for=".tile") as app:
        app.tab.evaluate(f"openItem({archive.ids['broken']})")
        app.wait_for(".noview")

        panel = app.tab.evaluate("document.querySelector('#viewer .noview').textContent")
        if _item(archive, archive.ids["broken"])["can_reencode"]:
            assert "could read" in panel, panel
        else:
            assert ".avi" in panel, "the message does not name the format it cannot play"
        # The way out is the point of the panel: another player can open it.
        assert app.count("#viewer .noview a.iwide") == 1
        # ...and the element that could not draw it is gone, not left behind it
        # holding the download open.
        assert app.count("#mmedia video") == 0
        assert app.errors() == []


def test_a_video_that_opens_and_draws_nothing_says_so_too(open_app, archive):
    """The failure with no error to catch, and the reason the stage listens
    twice.

    Motion JPEG in an old .mov, MPEG-4 Part 2 in a .3gp, HEVC from a recent
    phone: the window opens the container, cannot draw the video inside it, and
    reports no error at all. Metadata loads, the length is right, the transport
    runs, any sound plays -- and the picture never arrives. Watching `error`
    alone would leave every one of those exactly as black as before.

    This fixture is sound with no picture in it, so it reaches the panel down
    either route -- the window draws nothing, and re-encoding it puts no
    picture there either.
    """
    with open_app("library", wait_for=".tile") as app:
        app.tab.evaluate(f"openItem({archive.ids['soundonly']})")
        app.wait_for(".noview")

        panel = app.tab.evaluate("document.querySelector('#viewer .noview').textContent")
        if _item(archive, archive.ids["soundonly"])["can_reencode"]:
            assert "could read" in panel, panel
        else:
            # Not the other branch's wording: this file was read, and saying it
            # was of an unknown kind sends someone looking in the wrong place.
            assert "read the file" in panel, panel
        assert app.count("#mmedia video") == 0
        assert app.errors() == []


def test_a_video_the_window_can_play_is_left_alone(open_app, archive):
    """The guard on the two tests above: they would both pass on a stage that
    had simply stopped playing video."""
    with open_app("library", wait_for=".tile") as app:
        _open_first_photo(app)
        app.tab.evaluate(f"openItem({archive.ids['broken']})")
        app.wait_for(".noview")

        # Back to a file that draws: the panel must go with it.
        app.tab.evaluate(f"openItem({archive.ids['first_file']})")
        app.tab.wait_for(
            "!!document.querySelector('#mmedia img') && !document.querySelector('#viewer .noview')",
            what="the stage to go back to drawing the file it was given",
        )

        assert app.errors() == []


def test_a_re_encoded_video_gets_the_same_player_as_a_native_one(open_app, archive):
    """The viewer must not hand you two different players depending on how a
    file happens to be stored.

    A video the window will not open is re-encoded on the way out, and that
    stream has no length and nothing to rewind to -- so the native controls
    come off and a copy of them goes on, over the length the catalogue
    measured. The copy is the point: everything the native panel reaches, this
    reaches, or the difference is one the person watching has to care about.
    """
    fid = archive.ids.get("reencodable")
    if fid is None:
        pytest.skip("re-encoding for playback needs ffmpeg")
    with open_app("library", wait_for=".tile") as app:
        app.tab.evaluate(f"openItem({fid})")
        app.wait_for(".vxport")
        app.tab.wait_for(
            "(document.querySelector('#mmedia video') || {}).videoWidth > 0",
            what="the re-encoding to put a picture on the stage",
        )

        # Native controls off, or there are two transports over one video.
        assert app.tab.evaluate("document.querySelector('#mmedia video').controls") is False
        for control in ("vxplay", "vxbar", "vxtime", "vxmute", "vxvol", "vxfull", "vxmore"):
            assert app.count(f"#mmedia .vxport .{control}") == 1, f"no .{control} on the transport"
        # The length comes from the catalogue, not from the stream: the stream
        # does not know one, and a bar scaled to what has arrived so far
        # rescales under the pointer every few seconds.
        assert "0:02" in app.tab.evaluate(
            "document.querySelector('#mmedia .vxtime').textContent"
        ), "the transport is not drawn against the length the archive measured"
        assert app.errors() == []


# The two boxes that have to end up identical, as a JS expression returning a
# pair of [left, width, bottom]. Shared so the wait and the assertion below are
# asking about exactly the same thing.
_TRANSPORT_AND_PICTURE = (
    "['#mmedia video', '#mmedia .vxport'].map("
    "s => (r => [Math.round(r.left), Math.round(r.width), Math.round(r.bottom)])"
    "(document.querySelector(s).getBoundingClientRect()))"
)


def test_the_transport_sits_on_the_picture_not_on_the_stage(open_app, archive):
    """Where the native panel sits, so arrowing from one player to the other
    does not move the controls across the window.

    Waited for rather than read straight off: a video element is 300x150 until
    its metadata lands, and the transport is put up before that so there are
    controls during the second the re-encoding takes to start. It follows the
    picture to its real size a moment later, which is what is being checked --
    a resize observer doing its job, not the value it happens to hold on the
    frame the test looked.
    """
    fid = archive.ids.get("reencodable")
    if fid is None:
        pytest.skip("re-encoding for playback needs ffmpeg")
    with open_app("library", wait_for=".tile") as app:
        app.tab.evaluate(f"openItem({fid})")
        app.wait_for(".vxport")
        app.tab.wait_for(
            "(document.querySelector('#mmedia video') || {}).videoWidth > 0",
            what="the re-encoding to put a picture on the stage",
        )
        app.tab.wait_for(
            f"(([v, p]) => v[0] === p[0] && v[1] === p[1] && v[2] === p[2])({_TRANSPORT_AND_PICTURE})",
            what="the transport to settle onto the picture",
        )

        video, port = json.loads(app.tab.evaluate(f"JSON.stringify({_TRANSPORT_AND_PICTURE})"))
        assert port == video, f"the transport is not on the picture: {port} vs {video}"
        # ...and it is on the picture rather than merely on something small:
        # the stage is far wider, which is where it used to sit.
        stage = app.tab.evaluate("Math.round(document.getElementById('mmedia').offsetWidth)")
        assert port[1] < stage, "the transport is still the width of the stage"
        assert app.errors() == []


def test_a_video_being_converted_opens_at_its_real_size_over_its_own_frame(open_app, archive):
    """What the wait looks like while the re-encoding starts.

    A <video> with nothing decoded yet is 300x150 of black -- a small dark box
    adrift on the stage, at the wrong size and the wrong shape, for the whole
    of the second the conversion takes. Worse, the transport is laid on the
    picture's own edges, so it opened around that box and then jumped when the
    real dimensions landed.

    Both are answered from the catalogue, which measured this file at index
    time: the element carries the size it will keep, and the frame already
    extracted for the grid stands in until a real one arrives.

    The "Converting this video…" note itself is deliberately not raced for
    here. It is put up before the load and taken down by the first frame, and
    on a two-second 64x48 clip that gap is short enough that asserting on it
    would be a coin toss. What is checked is that it does not outlive the
    picture, which is the failure that would actually be seen.
    """
    fid = archive.ids.get("reencodable")
    if fid is None:
        pytest.skip("re-encoding for playback needs ffmpeg")
    with open_app("library", wait_for=".tile") as app:
        app.tab.evaluate(f"openItem({fid})")
        app.wait_for("#mmedia video")

        # The seeded clip is 64x48 and the catalogue knows it, so the element
        # must never be the 300x150 default.
        assert (
            app.tab.evaluate(
                "JSON.stringify([document.querySelector('#mmedia video').width,"
                " document.querySelector('#mmedia video').height])"
            )
            == "[64,48]"
        ), "the video opened at the element default instead of its own size"
        assert app.tab.evaluate(
            "(document.querySelector('#mmedia video').poster || '').includes('/thumb/')"
        ), "no frame standing in while the conversion runs"

        app.tab.wait_for(
            "(document.querySelector('#mmedia video') || {}).videoWidth > 0",
            what="the re-encoding to put a picture on the stage",
        )
        assert app.count("#mmedia .vxwait") == 0, "the note outlived the picture it was waiting for"
        assert app.errors() == []
