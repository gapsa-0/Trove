"""A place's own page: the page a person or a pet opens, for a place.

The map's side panel is a preview; this is where a place is looked through.
Reached two ways -- a card in "Every place" opens it, and so does the side
panel's "Open place" -- and left by the same back control as the other two,
onto a Places screen that was set aside rather than rebuilt.
"""

from __future__ import annotations


def _open_from_card(app) -> None:
    app.wait_for("#placegrid .pcard")
    app.click("#placegrid .pcard .placecollage")
    app.wait_for(".facetopbar .back-control")
    app.wait_for("#grid .tile")


def test_a_place_card_opens_the_place_page(open_app):
    """The page has the person page's parts: a way back, the name as a rename
    control, the count, recent changes, the actions menu, and the photos."""
    with open_app("places") as app:
        _open_from_card(app)
        bar = app.tab.evaluate(
            "(() => { const b = document.querySelector('.facetopbar'); return {"
            " name: b.querySelector('.ftb-name').textContent.trim(),"
            " count: b.querySelector('.ftb-count').textContent.trim(),"
            " history: !!b.querySelector('.histmenu[data-entity=place]'),"
            " menu: !!b.querySelector('.ftb-actions .cardmenu-trigger'),"
            " back: b.querySelector('.back-control').getAttribute('aria-label') }; })()"
        )
        assert bar["name"] == "Bariloche", bar
        assert bar["count"].startswith("12 "), bar
        assert bar["history"] and bar["menu"], bar
        assert bar["back"] == "Back to Places", bar
        assert app.count("#grid .tile") == 12
        assert app.errors() == []


def test_the_side_panel_opens_the_place_page(open_app):
    with open_app("places") as app:
        app.wait_for(".leaflet-marker-icon .mk")
        app.click(".leaflet-marker-icon")
        app.wait_for("#placeactions .quietbtn")
        app.click("#placeactions .quietbtn")
        app.wait_for(".facetopbar .back-control")
        assert app.text(".facetopbar .ftb-name").strip() == "Bariloche"
        assert app.errors() == []


def test_going_back_returns_to_the_places_screen_as_it_was_left(open_app):
    """Set aside, not rebuilt: the map is the same Leaflet instance at the same
    view, and it has been told its size again -- a map measured while detached
    from the page draws into no box at all."""
    with open_app("places") as app:
        app.wait_for(".leaflet-marker-icon .mk")
        app.tab.evaluate("document.getElementById('lmap').dataset.probe = 'kept'")
        _open_from_card(app)
        app.click(".facetopbar .back-control")
        app.wait_for("#placegrid .pcard")
        app.tab.wait_for(
            "document.querySelectorAll('.leaflet-marker-icon .mk').length > 0",
            what="the map to draw its markers again",
        )
        kept = app.tab.evaluate(
            "(() => { const m = document.getElementById('lmap'); return {"
            " probe: m.dataset.probe, height: m.getBoundingClientRect().height }; })()"
        )
        assert kept["probe"] == "kept", "the Places screen was rebuilt rather than restored"
        assert kept["height"] > 0, kept
        assert app.errors() == []


def test_renaming_on_the_page_reaches_the_places_screen(open_app):
    with open_app("places") as app:
        _open_from_card(app)
        app.click("#placename .person-name-button")
        app.wait_for("#placename input")
        app.tab.evaluate(
            "(() => { const i = document.querySelector('#placename input');"
            " i.value = 'Cerro Catedral'; i.dispatchEvent(new Event('blur')); })()"
        )
        app.wait_for_text("Cerro Catedral")
        app.click(".facetopbar .back-control")
        app.tab.wait_for(
            "document.getElementById('placegrid')?.textContent.includes('Cerro Catedral')",
            what="the restored gallery to carry the new name",
        )
        assert app.errors() == []
