"""The documentation renderer's contract.

``web/markdown.py`` covers a subset of Markdown rather than the language, which
is only safe if the boundary is explicit: an unsupported construct has to arrive
on the page as visible literal text, never vanish from it. That is what most of
these check, alongside the escaping and the link filter -- the input is written
in this repository, so the point is that a mistake in a page is visible rather
than exploitable.
"""

from __future__ import annotations

from trove.web.markdown import render, slug


def test_front_matter_is_read_and_kept_out_of_the_body():
    page = render("---\ntitle: Duplicates\nfeature: duplicates\n---\n\nHello.\n")
    assert page.meta == {"title": "Duplicates", "feature": "duplicates"}
    assert page.html == "<p>Hello.</p>"


def test_unterminated_front_matter_is_body_rather_than_swallowed():
    """The failure mode worth avoiding is a page rendering blank because its
    front matter lost a fence."""
    page = render("---\ntitle: Nope\n\nStill here.\n")
    assert page.meta == {}
    assert "Still here." in page.html


def test_headings_carry_anchors_and_h2s_become_the_outline():
    page = render("# Title\n\n## How it works\n\n### Detail\n\n## The numbers\n")
    assert '<h2 id="how-it-works">How it works</h2>' in page.html
    assert '<h3 id="detail">Detail</h3>' in page.html
    assert page.outline == [("how-it-works", "How it works"), ("the-numbers", "The numbers")]


def test_slug_drops_punctuation_rather_than_encoding_it():
    assert slug("What it gets wrong") == "what-it-gets-wrong"
    assert slug("Which copy is kept?") == "which-copy-is-kept"
    assert slug("!!!") == "section"


def test_pipe_tables_render_with_the_alignment_row_consumed():
    page = render("| Setting | Value |\n| --- | --- |\n| `a` | 6 |\n| `b` | 7 |\n")
    assert page.html.count("<tr>") == 3
    assert "<th>Setting</th>" in page.html
    assert "<td><code>a</code></td>" in page.html
    assert "---" not in page.html


def test_a_rule_between_paragraphs_is_still_a_rule():
    """``---`` is both a horizontal rule and a table's alignment row, and only
    the open-table state tells them apart."""
    page = render("One.\n\n---\n\nTwo.\n")
    assert page.html == "<p>One.</p><hr><p>Two.</p>"


def test_lists_take_their_wrapped_continuation_lines():
    page = render("- first item\n  wrapped on\n- second\n")
    assert page.html == "<ul><li>first item wrapped on</li><li>second</li></ul>"


def test_ordered_and_unordered_lists_do_not_merge():
    page = render("- a\n\n1. b\n")
    assert page.html == "<ul><li>a</li></ul><ol><li>b</li></ol>"


def test_inline_spans():
    page = render("**bold** and *thin* and `code`\n")
    assert page.html == "<p><strong>bold</strong> and <em>thin</em> and <code>code</code></p>"


def test_markup_in_the_source_is_escaped_not_rendered():
    page = render("A <script>alert(1)</script> and 5 < 6 & 7.\n")
    assert "<script>" not in page.html
    assert "&lt;script&gt;" in page.html
    assert "5 &lt; 6 &amp; 7" in page.html


def test_a_page_link_becomes_this_app_route():
    """Pages link each other as ``file.md`` so they read on a Git host too."""
    page = render("See [Duplicates](duplicates.md) and [the cut](people.md#the-numbers).\n")
    assert 'href="#/docs/duplicates"' in page.html
    assert 'href="#/docs/people#the-numbers"' in page.html


def test_an_external_link_opens_away_from_the_app():
    page = render("[SigLIP](https://example.com/x)\n")
    assert 'target="_blank"' in page.html and 'rel="noopener noreferrer"' in page.html


def test_an_unusable_link_target_keeps_its_words_and_loses_its_href():
    for target in ("javascript:alert(1)", "data:text/html,x", "http://plain.example"):
        page = render(f"[press me]({target})\n")
        assert "<a " not in page.html, target
        assert "press me" in page.html, target


def test_a_code_fence_is_escaped_and_keeps_its_lines():
    page = render('```json\n{"a": "<b>"}\n{}\n```\n')
    assert '<pre><code class="lang-json">' in page.html
    assert "&lt;b&gt;" in page.html
    assert page.html.count("\n") == 1  # the two body lines, joined by one newline


def test_a_scale_fence_becomes_a_figure_positioned_from_its_range():
    page = render(
        "```scale\n"
        "range 0 64\n"
        "band 0 6 Same photo\n"
        "mark 6 phash_hamming_threshold\n"
        "note Hamming distance.\n"
        "```\n"
    )
    assert 'class="doc-scale"' in page.html
    # 0..6 of a 0..64 range is the left 9.375%, and the mark sits at its edge.
    assert "left:0%;width:9.375%" in page.html
    assert "left:9.375%" in page.html
    assert "phash_hamming_threshold" in page.html
    assert "<figcaption>Hamming distance.</figcaption>" in page.html
    # The label is listed under the track, with the range it covers.
    assert "<span>Same photo</span><b>0&ndash;6</b>" in page.html


def test_a_bands_tone_is_stated_by_the_page_not_inferred_from_its_position():
    """Drawing the first band grey said the opposite of what the one band on
    the Duplicates figure means."""
    page = render(
        "```scale\nband 0 .3 muted Different\nband .3 .6 soft Maybe\nband .8 1 Same\n```\n"
    )
    assert 'class="doc-scale-band muted"' in page.html
    assert 'class="doc-scale-band soft"' in page.html
    assert 'class="doc-scale-band "' in page.html
    assert '<i class="muted"></i><span>Different</span>' in page.html


def test_a_label_starting_with_an_unknown_word_keeps_all_of_it():
    page = render("```scale\nband 0 1 Muddled and grey\n```\n")
    assert "<span>Muddled and grey</span>" in page.html
    assert 'class="doc-scale-band "' in page.html


def test_a_scale_value_is_written_the_way_it_was_typed():
    page = render("```scale\nrange 0 1\nmark 0.75 x\n```\n")
    assert ">0.75<" in page.html
    assert "0.750000" not in page.html


def test_blockquotes_join_their_own_lines():
    page = render("> one\n> two\n\nafter\n")
    assert page.html == "<blockquote><p>one two</p></blockquote><p>after</p>"
