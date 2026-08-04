"""The reference pages: which ones exist, in what order, and what they say.

The pages themselves are Markdown files in ``web/docs/``, so the thing that
explains how duplicate grouping works is a file a reader can also open in the
repository, and the same words serve both. ``markdown.py`` turns one into HTML;
this module decides what the set of them *is* and joins it to the rest of the
app.

Two joins matter, and both exist to stop the documentation drifting away from
the thing it documents:

* **Order is the pipeline's order.** ``ORDER`` interleaves the feature pages
  with the two that belong to no stage, and the feature pages must appear in
  ``features.FEATURES`` order -- which is the order the work actually runs in,
  and the order the setup panel and the Overview chain already draw. A reader
  moving down the index rail is moving along the pipeline.
* **Facts with a home in the code are not retyped.** A page writes
  ``{{download_mb}}`` rather than "689 MB", and ``{{label}}`` rather than the
  feature's name, so a feature that is renamed or whose weights change size
  cannot leave a stale number behind on its own documentation page.

``tests/unit/test_docs.py`` checks both directions of the first join and that
every page's ``feature:`` names a real feature.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .. import features
from .markdown import Page, render

DOCS_DIR = Path(__file__).with_name("docs")

# Every page, in reading order. The feature-bearing ones are in pipeline order;
# the three that are not stages bracket them -- the guide someone reads before
# adding a folder, then the promise the whole design rests on and the questions
# that come up once it is running.
ORDER: tuple[str, ...] = (
    "index",
    "indexing",
    "duplicates",
    "people",
    "pets",
    "places",
    "search",
    "privacy",
    "faq",
)

_SLUG_OK = re.compile(r"\A[a-z0-9-]+\Z")
_TOKEN = re.compile(r"\{\{(\w+)\}\}")


@dataclass(frozen=True)
class Entry:
    """One page as the index rail needs it, before its body is read."""

    slug: str
    title: str
    summary: str
    # The feature this page documents, or "" for the pages that document no
    # single stage. The rail draws the ones that have a feature as a chain and
    # the ones that don't as plain links, because that is the real difference
    # between them.
    feature: str
    # Whether the feature is one an archive cannot decline. The rail marks the
    # trunk of the pipeline apart from what clips onto it, the same distinction
    # ADR 0015 draws and the Overview already shows.
    always_runs: bool


def _substitute(text: str, feature: features.Feature | None) -> str:
    """Replace ``{{token}}`` with what the feature catalogue says, not with prose.

    An unknown token is left exactly as written rather than blanked: a typo
    should be visible on the page, not silently turn a sentence into a gap.
    """
    if feature is None:
        return text
    values = {
        "label": feature.label,
        "tagline": feature.tagline,
        "download_mb": str(feature.download_mb),
    }
    return _TOKEN.sub(lambda m: values.get(m.group(1), m.group(0)), text)


def _read(slug: str) -> Page | None:
    """One page, rendered, or ``None`` if there is no such file.

    Read from disk per request rather than cached at import: these are a few
    kilobytes on a local disk, and it means editing a page and reloading shows
    the edit -- the same reason the shell and the stylesheets are served
    ``no-store``.
    """
    path = DOCS_DIR / f"{slug}.md"
    if not _SLUG_OK.match(slug) or not path.is_file():
        return None
    page = render(path.read_text(encoding="utf-8"))
    feature = features.by_id(page.meta.get("feature", ""))
    if feature is None:
        return page
    return Page(
        meta=page.meta,
        html=_substitute(page.html, feature),
        outline=page.outline,
    )


def catalogue() -> list[Entry]:
    """Every page in reading order, with what the index rail draws.

    A slug in ``ORDER`` with no file behind it is skipped rather than raising:
    the catalogue is what the rail renders, and one missing file should cost one
    entry, not the whole screen. The test is what makes that not happen quietly.
    """
    entries = []
    for slug in ORDER:
        page = _read(slug)
        if page is None:
            continue
        feature = features.by_id(page.meta.get("feature", ""))
        entries.append(
            Entry(
                slug=slug,
                title=page.meta.get("title", slug),
                summary=page.meta.get("summary", ""),
                feature=feature.id if feature else "",
                always_runs=bool(feature and feature.required),
            )
        )
    return entries


def page(slug: str) -> dict | None:
    """One rendered page as the reader needs it, or ``None`` if it doesn't exist."""
    doc = _read(slug)
    if doc is None:
        return None
    feature = features.by_id(doc.meta.get("feature", ""))
    at = ORDER.index(slug) if slug in ORDER else -1
    return {
        "slug": slug,
        "title": doc.meta.get("title", slug),
        "summary": doc.meta.get("summary", ""),
        "html": doc.html,
        "outline": [{"id": anchor, "text": text} for anchor, text in doc.outline],
        # What the page's eyebrow states: which feature it documents, its mark,
        # and whether that work is optional. All four come from the feature
        # catalogue so the documentation page for Duplicates is visibly the same
        # thing as the setup card and the Overview card that share its name.
        "feature": feature.id if feature else "",
        "feature_label": feature.label if feature else "",
        "icon": feature.icon if feature else "",
        "always_runs": bool(feature and feature.required),
        "download_mb": feature.download_mb if feature else 0,
        # Reading order, so the article can offer the next page without the
        # frontend having to know what the order is.
        "prev": ORDER[at - 1] if at > 0 else "",
        "next": ORDER[at + 1] if 0 <= at < len(ORDER) - 1 else "",
    }
