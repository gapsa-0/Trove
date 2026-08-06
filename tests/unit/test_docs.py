"""The reference pages, and the joins that stop them going stale.

Documentation that names a threshold is confidently wrong the moment somebody
tunes one, which is worse than having none. So the numbers a page prints are
checked against the constants they describe, and its place in the pipeline is
checked against the feature catalogue. Both are the same move
``tests/unit/test_features.py`` makes for the string tables in ``features.py``:
the agreement is asserted rather than assumed.
"""

from __future__ import annotations

import dataclasses
import re

import pytest

from trove import features
from trove.config import Config
from trove.web import docs
from trove.web.markdown import render

# A settings-table row: `| `name` | value | what it does |`.
_SETTING_ROW = re.compile(r"\A\|\s*`([a-z0-9_]+)`\s*\|\s*(.+?)\s*\|")
# A calibration figure's cut, whose label is usually the setting it is.
_SCALE_MARK = re.compile(r"\Amark\s+(\S+)\s+([a-z0-9_]+)\s*\Z")
_MD_LINK = re.compile(r"\]\((?!https://)([a-z0-9-]+)\.md(?:#([\w-]+))?\)")

DEFAULTS = {f.name: getattr(Config(), f.name) for f in dataclasses.fields(Config)}
SLUGS = [p.stem for p in sorted(docs.DOCS_DIR.glob("*.md"))]


def source(slug: str) -> str:
    return (docs.DOCS_DIR / f"{slug}.md").read_text(encoding="utf-8")


def cited(slug: str) -> list[tuple[str, str, str]]:
    """Every ``(setting, printed value, where)`` a page states, table or figure."""
    out = []
    for line in source(slug).splitlines():
        row = _SETTING_ROW.match(line.strip())
        if row and row.group(1) in DEFAULTS:
            out.append((row.group(1), row.group(2), "table"))
        mark = _SCALE_MARK.match(line.strip())
        if mark and mark.group(2) in DEFAULTS:
            out.append((mark.group(2), mark.group(1), "figure"))
    return out


def states(printed: str, default: object) -> bool:
    """Whether a printed cell says the same thing as the default it describes."""
    printed = printed.replace("`", "").strip()
    if isinstance(default, bool):
        return printed.lower() == str(default).lower()
    if default is None:
        return printed.lower() in {"unset", "none", "-"}
    if isinstance(default, list):
        return [p.strip() for p in printed.split(",")] == [str(v) for v in default]
    if isinstance(default, int | float):
        try:
            return float(printed) == float(default)
        except ValueError:
            return False
    return printed == str(default)


# -- the numbers -------------------------------------------------------------


@pytest.mark.parametrize("slug", SLUGS)
def test_every_setting_a_page_prints_matches_its_default(slug):
    """The guard this whole file exists for: retune a threshold without editing
    its page and this fails, instead of the app quietly documenting the old one."""
    for name, printed, where in cited(slug):
        assert states(printed, DEFAULTS[name]), (
            f"{slug}.md ({where}) says {name} is {printed!r}, but the default is {DEFAULTS[name]!r}"
        )


def test_the_pages_between_them_document_the_thresholds_that_matter():
    """A spot check that the guard above has something to bite on -- it passes
    trivially if the pages stop naming settings at all."""
    named = {name for slug in SLUGS for name, _, _ in cited(slug)}
    assert {
        "phash_hamming_threshold",
        "faces_fiqa_high",
        "faces_core_link_sim",
        "pets_cluster_similarity",
        "pets_human_iou",
        "place_min_media",
        "semantic_search_min_similarity",
    } <= named


def test_a_download_size_is_never_typed_into_a_page():
    """Sizes come from the feature catalogue through ``{{download_mb}}``. Typed
    in, they would be four more numbers with no owner."""
    for slug in SLUGS:
        feature = features.by_id(render(source(slug)).meta.get("feature", ""))
        if feature is None or not feature.download_mb:
            continue
        assert "{{download_mb}}" in source(slug), f"{slug}.md should use the token"
        assert str(feature.download_mb) in docs.page(slug)["html"]


# -- the catalogue -----------------------------------------------------------


def test_order_and_the_directory_hold_exactly_the_same_pages():
    assert sorted(docs.ORDER) == SLUGS


def test_the_stage_pages_are_in_the_order_the_stages_run():
    """The index rail draws them as a chain, so their order has to be the
    pipeline's -- the same order the setup panel and the Overview already use."""
    by_slug = {e.slug: e.feature for e in docs.catalogue() if e.feature}
    documented = [by_slug[s] for s in docs.ORDER if s in by_slug]
    assert documented == [f.id for f in features.FEATURES if f.id in set(documented)]


def test_every_feature_gets_a_page_and_every_page_names_a_real_feature():
    covered = {e.feature for e in docs.catalogue() if e.feature}
    assert covered == set(features.ids())


def test_a_feature_page_is_titled_with_the_feature_it_documents():
    """The rail is a fifth surface naming this work, and the only one whose words
    are typed rather than composed: ``docs.py`` serves ``feature_label`` from the
    catalogue for the heading but takes ``title`` from the frontmatter verbatim.
    Both text pages were still titled "Documents" and "Pictures of text" after the
    features were renamed, which is exactly the drift ADR 0021 forbids -- so this
    is the check that a hand-typed title cannot outlive the label it copies."""
    for entry in docs.catalogue():
        if not entry.feature:
            continue
        feature = features.by_id(entry.feature)
        assert entry.title == feature.label, entry.slug


def test_a_page_that_documents_a_feature_reports_whether_it_is_optional():
    entries = {e.slug: e for e in docs.catalogue()}
    assert entries["duplicates"].always_runs is True
    assert entries["people"].always_runs is False
    assert entries["privacy"].always_runs is False


@pytest.mark.parametrize("slug", SLUGS)
def test_every_page_has_the_front_matter_the_rail_reads(slug):
    meta = render(source(slug)).meta
    assert meta.get("title"), slug
    assert meta.get("summary"), slug


@pytest.mark.parametrize("slug", SLUGS)
def test_every_page_link_points_at_a_page_that_exists(slug):
    for target, anchor in _MD_LINK.findall(source(slug)):
        assert target in SLUGS, f"{slug}.md links to missing {target}.md"
        if anchor:
            assert anchor in {a for a, _ in render(source(target)).outline}, (
                f"{slug}.md links to {target}.md#{anchor}, which has no such heading"
            )


# -- serving -----------------------------------------------------------------


def test_an_unknown_or_traversing_slug_is_simply_absent():
    for bad in ("nope", "../settings", "index.md", "", "INDEX"):
        assert docs.page(bad) is None, bad


def test_a_page_carries_its_neighbours_so_the_reader_can_walk_the_chain():
    first, last = docs.page(docs.ORDER[0]), docs.page(docs.ORDER[-1])
    assert first["prev"] == "" and first["next"] == docs.ORDER[1]
    assert last["next"] == "" and last["prev"] == docs.ORDER[-2]


def test_a_feature_page_carries_the_catalogues_own_name_and_mark():
    """The page, the setup card and the Overview card are one thing with one
    name; ``features.py`` says so and this is that promise reaching the page."""
    page = docs.page("search")
    semantic = features.by_id("semantic")
    assert page["feature_label"] == semantic.label
    assert page["icon"] == semantic.icon
    assert page["download_mb"] == semantic.download_mb


def test_every_feature_has_a_page_that_documents_it():
    """The frontend used to keep its own feature-to-page table, and it was
    missing the three that arrived with document text -- so choosing them on the
    setup panel was the one decision made with no way to read what it does
    first. Derived from the pages' own frontmatter now, so a feature added
    without a page fails here rather than losing its link quietly."""
    for feature_id in features.ids():
        assert docs.slug_for_feature(feature_id), f"{feature_id} has no page"


def test_a_feature_nothing_documents_reports_no_page():
    assert docs.slug_for_feature("not-a-feature") == ""
