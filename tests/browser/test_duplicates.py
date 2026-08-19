"""The Duplicates screen, driven in a real browser.

Split out of ``test_screens.py`` once this screen had six tests of its own: that
module's question is "does every screen render", which is one test with a
parameter, and everything else there was already about a particular screen.

What these check is what only this tier can see -- that a group is drawn as a
set of controls a person can actually use, that a copy's own name and folder
survive whatever they are called, and that the screen does not go on reporting
figures the server has since replaced.
"""

from __future__ import annotations


def test_opening_a_copy_bounds_the_arrows_to_its_own_group(open_app):
    """The viewer walks S.gallery, so what a screen puts there is a claim about
    what "next" means. Filling it with every copy on the page ran the arrows off
    the end of the group being compared and into the next group's photographs,
    which are a different picture entirely.
    """
    with open_app("dups", wait_for=".duptile") as app:
        # Two groups have to be on the page for the question to exist at all:
        # with one, "this group" and "everything shown" are the same set and
        # the old behaviour and the new one are indistinguishable. The fixture
        # seeds both (see conftest's _seed_duplicates and _seed_hostile_names),
        # which is better than the clone this used to make -- a cloned tile
        # carries no click handler now that the tiles are built as real buttons
        # rather than markup with an inline `onclick`.
        counts = app.tab.evaluate("""
          (() => [
            document.querySelectorAll('.dupgroup').length,
            document.querySelectorAll('.duptile[data-file-id]').length,
          ])()
        """)
        assert counts == [2, 4], f"expected two groups of two, got {counts}"

        app.tab.evaluate("document.querySelector('.duptile').click()")
        app.wait_for(".vpos b")
        readout = app.text(".vpos")

        # Two, not four: the arrows stop at the end of the group.
        assert "of" in readout and "2" in readout, readout
        assert "in this duplicate group" in readout, readout
        assert app.errors() == []


def test_the_listing_can_be_filtered_and_sorted(open_app):
    """Both controls send the listing back to the server rather than hiding
    rows: the list is paged, so most matching groups are still there."""
    with open_app("dups", wait_for=".duptile") as app:
        requested = app.tab.evaluate("""
          (() => {
            const seen = [];
            const original = window.fetch;
            window.fetch = (...args) => { seen.push(String(args[0])); return original(...args); };
            document.getElementById('dup-match').value = 'visual';
            document.getElementById('dup-match').dispatchEvent(new Event('change'));
            document.getElementById('dup-sort').value = 'count_asc';
            document.getElementById('dup-sort').dispatchEvent(new Event('change'));
            window.fetch = original;
            return JSON.stringify(seen.filter(u => u.includes('/api/dups')));
          })()
        """)
        import json

        urls = json.loads(requested)
        assert any("match=visual" in u for u in urls), urls
        assert any("sort=count_asc" in u for u in urls), urls
        app.wait_for("#dup-count")
        assert app.errors() == []


def test_a_group_wraps_onto_a_second_line_instead_of_scrolling_sideways(open_app):
    """A group is a set of copies to compare against each other, and a copy
    parked off the right edge cannot be compared with anything. Asserted as
    layout rather than markup: the tiles are injected here so the rule is
    checked against a group big enough to overflow, which the fixture's pair
    never is.
    """
    with open_app("dups", wait_for=".duptile") as app:
        rows = app.tab.evaluate("""
          (() => {
            const row = document.querySelector('.duprow');
            const tile = row.querySelector('.duptile');
            for (let i = 0; i < 30; i++) row.appendChild(tile.cloneNode(true));
            const tops = new Set([...row.querySelectorAll('.duptile')]
              .map(t => t.getBoundingClientRect().top));
            return [tops.size, row.scrollWidth <= row.clientWidth];
          })()
        """)
        lines, fits = rows
        assert lines > 1, "30 copies stayed on one line"
        assert fits, "the group still overflows its own width"
        assert app.errors() == []


def test_a_duplicate_tile_is_a_control_that_names_its_file(open_app):
    """Every other grid in the app is built from tiles.js's `tile()`, which is a
    real button carrying the file's name. This screen hand-rolled its own out of
    a `<div onclick>` with only a truncated folder under it, so 150 pictures on
    a page were unreachable by keyboard and, in a group of nine copies of one
    photograph, indistinguishable from each other.
    """
    with open_app("dups", wait_for=".duptile") as app:
        shape = app.tab.evaluate("""
          (() => {
            const t = document.querySelector('.duptile');
            return {
              tag: t.tagName,
              type: t.type,
              label: t.getAttribute('aria-label') || '',
              name: (t.querySelector('.dtname') || {}).textContent || '',
              folder: (t.querySelector('.dtpath') || {}).textContent || '',
            };
          })()
        """)

        assert shape["tag"] == "BUTTON" and shape["type"] == "button"
        assert shape["name"].endswith(".jpg"), shape
        assert shape["folder"], "a copy has to say where it lives"
        # The label carries what the tile says, so the picture is not announced
        # as an unnamed image.
        assert ".jpg" in shape["label"] and "Kept" in shape["label"], shape
        assert app.errors() == []


def test_a_folder_with_a_quotation_mark_in_its_name_renders_as_text(open_app, archive):
    """The regression: `title="${mm.folder}"`.

    A folder called `Fotos de "Mama" & Papa <2015>` closed the attribute at its
    first quote, so the rest became junk attributes, the tile's `onclick` was
    consumed as text, and the leftover markup was drawn on the page. The tile
    stopped opening -- the one copy in the group you could not look at.
    """
    with open_app("dups", wait_for=".duptile") as app:
        found = app.tab.evaluate(f"""
          (() => {{
            const t = document.querySelector('.duptile[data-file-id="{archive.ids["hostile_copy"]}"]');
            if (!t) return null;
            return {{
              title: t.title,
              folder: (t.querySelector('.dtpath') || {{}}).textContent || '',
              attrs: [...t.attributes].map(a => a.name).sort(),
              opens: typeof t.onclick === 'function',
              stray: t.textContent.includes('onclick') || t.textContent.includes('openDupCopy'),
            }};
          }})()
        """)

        assert found is not None, "the awkwardly-named copy is not on the page"
        # The name survives whole, in the attribute and in the caption.
        assert '"' in found["title"] or "&" in found["title"], found["title"]
        assert "&" in found["folder"], found["folder"]
        # Nothing of the name leaked into the tag itself.
        assert found["attrs"] == ["aria-label", "class", "data-file-id", "title", "type"], found
        assert found["opens"], "the tile lost its click handler to its own folder name"
        assert not found["stray"], "markup leaked into the page as text"
        assert app.errors() == []


def test_the_screen_asks_again_when_it_is_returned_to(open_app):
    """Grouping is scheduled, not pressed, so this screen is routinely opened
    while it is still running -- and the shell replays a stashed section rather
    than re-rendering it, so what it was left showing is what comes back. It
    used to come back stale for the rest of the session: "0 groups" and "no
    copies found yet" beside a sidebar reading "Up to date".
    """
    with open_app("dups", wait_for=".duptile") as app:
        app.tab.evaluate("document.getElementById('dup-groups').textContent = '0'")
        app.show_section("overview")
        app.wait_until_loaded()
        app.show_section("dups")
        app.wait_for(".duptile")
        app.tab.wait_for(
            "document.getElementById('dup-groups').textContent !== '0'",
            what="the stashed screen to ask the server again on the way back",
        )

        assert app.errors() == []


def test_a_second_copy_can_be_kept_and_the_last_one_cannot_be_dropped(open_app):
    """Trove picks which copy to show, and that is a ranking rather than a
    verdict: the "worse" copy can be the one already in the album that gets
    shared, and two copies of what grouping called the same picture are
    sometimes two pictures.

    Both halves are asserted here because either alone is a different feature:
    that a second copy can be kept, and that a group can never be left showing
    none of its copies -- the toggle on the last kept one is dead and says so.
    """
    with open_app("dups") as app:
        app.wait_for(".dupgroup .dupkeep")
        # One group, named: the screen lists several, and a count across all of
        # them would move for reasons this test is not about.
        group = ".dupgroup:first-of-type"
        kept = app.tab.evaluate(
            f"[...document.querySelectorAll('{group} .dupkeep')]"
            ".map(b => b.getAttribute('aria-checked'))"
        )
        assert kept.count("true") == 1, f"the group did not start with one kept copy: {kept}"
        assert app.tab.evaluate(
            f"document.querySelector('{group} .dupkeep[aria-checked=\"true\"]').disabled"
        ), "the only kept copy could be dropped, leaving the group showing nothing"

        app.click(f'{group} .dupkeep[aria-checked="false"]')

        app.tab.wait_for(
            f"document.querySelectorAll('{group} .dupkeep[aria-checked=\"true\"]').length === 2",
            timeout=10.0,
            what="the second copy to be kept",
        )
        # ...and with two kept, either may now be dropped again.
        assert app.count(f'{group} .dupkeep[aria-checked="true"]:disabled') == 0
        assert app.errors() == []
