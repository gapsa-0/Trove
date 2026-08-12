"""The viewer's video stage: the formats this window cannot draw, and what it
does about them.

Split from test_viewer.py at the seam the frontend is split at
(static/js/item-video.js, static/css/viewer-video.css): that module asks whether
the viewer opens, moves and stays quiet about work the archive never asked for,
this one asks what happens when the player built into the window is handed a
file it has no reader for -- the panel that says so, the re-encoded stream that
answers it, and the transport drawn over one.

Every test that needs the re-encoding path skips without an ffmpeg to run it,
because that is a fact about the machine and not about Trove.
"""

import json
import urllib.request

import pytest


def _open_first_photo(app):
    """Open the first tile in Browse and wait for the panel to be drawn.

    Same helper as test_viewer.py, kept here rather than shared: these two
    modules are read apart, and a three-line wait is cheaper duplicated than
    chased into a third file.
    """
    app.tab.evaluate("document.querySelector('#grid .tile').click()")
    app.tab.wait_for(
        "!!(document.querySelector('#modal.open #minfo h3') || {}).textContent",
        what="the panel to name the file it opened",
    )


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
        # rescales under the pointer every few seconds. Asked of the payload
        # rather than written in, so lengthening the seeded clip -- which the
        # drag test needs -- cannot quietly leave this asserting the old one.
        seconds = int(_item(archive, fid)["meta"]["duration_s"])
        assert app.tab.evaluate("document.querySelector('#mmedia .vxtime').textContent").endswith(
            f"/ {seconds // 60}:{seconds % 60:02d}"
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

    The "Loading…" note itself is deliberately not raced for
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


def test_leaving_a_video_mid_load_stops_it(open_app, archive):
    """Detaching a <video> is not the same as stopping it.

    Emptying the stage takes the element out of the document and leaves its
    load running; for a re-encoded video that load is an ffmpeg process, so
    arrowing through a folder of .avi files left one encoder running per file
    passed, each holding most of a core. Four or five of those is a machine
    that has stopped answering -- which is what this looked like from the
    outside, and it was reported as the app breaking.

    Asserted on the element that was left behind rather than on any process:
    clearing the source and reloading is what aborts the request in flight,
    and that is the thing the fix has to keep doing.
    """
    fid = archive.ids.get("reencodable")
    if fid is None:
        pytest.skip("re-encoding for playback needs ffmpeg")
    with open_app("library", wait_for=".tile") as app:
        app.tab.evaluate(f"openItem({fid})")
        app.wait_for("#mmedia video")
        app.tab.evaluate("window.__left = document.querySelector('#mmedia video'); 1")

        app.tab.evaluate(f"openItem({archive.ids['first_file']})")
        app.tab.wait_for(
            "!!document.querySelector('#mmedia img')",
            what="the viewer to move on to the photograph",
        )

        assert app.tab.evaluate("window.__left.getAttribute('src') === null"), (
            "the abandoned video kept its source, and with it the request behind it"
        )
        assert app.tab.evaluate("window.__left.paused") is True
        assert app.count("#mmedia video") == 0
        assert app.errors() == []


def test_opening_another_video_mid_load_leaves_one_player(open_app, archive):
    """Two videos in quick succession used to strand pieces of the first over
    the second: its "Loading…" was cleared by the one it had replaced, whose
    events go on arriving long after it is gone."""
    fid = archive.ids.get("reencodable")
    if fid is None:
        pytest.skip("re-encoding for playback needs ffmpeg")
    with open_app("library", wait_for=".tile") as app:
        app.tab.evaluate(f"openItem({fid})")
        app.wait_for("#mmedia video")
        app.tab.evaluate(f"openItem({archive.ids['broken']})")
        app.tab.evaluate(f"openItem({fid})")
        app.tab.wait_for(
            "(document.querySelector('#mmedia video') || {}).videoWidth > 0",
            what="the last video opened to put a picture up",
        )

        assert app.count("#mmedia video") == 1
        assert app.count("#mmedia .vxport") == 1, "a transport was left over from an earlier file"
        # The panel belongs to the file that could not be drawn, which is no
        # longer the one on screen.
        assert app.count("#viewer .noview") == 0
        app.tab.wait_for(
            "document.querySelectorAll('#mmedia .vxwait').length === 0",
            what="the note to go with the picture it was waiting for",
        )
        assert app.errors() == []


def test_the_track_can_be_dragged_and_seeks_once_at_the_end(open_app, archive):
    """The last thing that made this feel like a different control.

    A click was handled and a drag was not, so the one gesture everybody uses
    on a video did nothing. It cannot simply be wired to seek on every move
    either: a seek here is a second of re-encoding, so a drag across the bar
    would queue an encoder per pointer position to answer a gesture that ends
    somewhere else entirely.

    So the bar previews while the pointer moves -- readout and handle follow,
    picture stays put -- and the seek happens once, on release, at the position
    actually chosen. Both halves are asserted: that the preview moves, and that
    the source does not move with it.

    Driven through the input pipeline rather than with dispatched events, for
    the same reason `App.hover` is: pointer capture and `:hover` follow the
    browser's own pointer, which a synthetic event does not move.
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
        track = app.tab.evaluate(
            "(r => [r.x, r.y + r.height / 2, r.width])"
            "(document.querySelector('#mmedia .vxbar').getBoundingClientRect())"
        )
        at = lambda frac: {  # noqa: E731 - a coordinate, not a policy
            "x": track[0] + track[2] * frac,
            "y": track[1],
            "button": "left",
            "clickCount": 1,
        }
        src = lambda: app.tab.evaluate("document.querySelector('#mmedia video').src")  # noqa: E731
        readout = lambda: app.tab.evaluate(  # noqa: E731
            "document.querySelector('#mmedia .vxtime').textContent"
        )
        before = src()

        app.tab.call("Input.dispatchMouseEvent", {"type": "mousePressed", **at(0.15)})
        pressed = readout()
        app.tab.call("Input.dispatchMouseEvent", {"type": "mouseMoved", **at(0.85)})
        dragged = readout()

        # Against the position dragged to, not merely "it changed": the clip is
        # playing while this runs, so a readout that ignored the pointer
        # entirely would still tick over on its own and pass that.
        # 15% and 85% of eight seconds are 0:01 and 0:06, and playback needs
        # six seconds to reach the second of those.
        assert pressed.startswith("0:01"), f"the press did not preview its position ({pressed})"
        assert dragged.startswith("0:06"), f"the readout did not follow the drag ({dragged})"
        assert src() == before, "the drag re-encoded on the way past, once per pointer position"

        app.tab.call("Input.dispatchMouseEvent", {"type": "mouseReleased", **at(0.85)})
        app.tab.wait_for(
            "document.querySelector('#mmedia video').src.includes('t=')",
            what="the release to seek, once",
        )

        assert app.errors() == []
