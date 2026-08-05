"""What an archive can be asked to do, as one table.

An archive is not obliged to run every stage this app knows how to run. A
folder of scanned paperwork has no faces worth clustering; a phone dump full of
untagged photos has no coordinates to map; and search by description costs a
689 MB download that someone who only wants duplicates removed should never be
asked to pay. So the work is offered as *features*, chosen per archive when it
is created and changeable afterwards.

Every consumer reads this one table:

* ``pipeline/stages.py`` resolves it to the stage kinds an archive may run, and
  a stage nobody enabled is never scheduled (see the availability note in that
  module).
* ``services/models.py`` resolves it to the weights an archive owes, which is
  what stops a feature nobody chose downloading anything: both the fetch job
  that gets them when the archive is created and the stage that would otherwise
  get them on first use read this same enabled set.
* ``config/archives.py`` stores the chosen ids on the archive's registry entry.
* ``services/archives.py`` adds what only a running installation knows -- is the
  dependency importable, are the weights already on disk -- and serves the
  result to the setup panel.

The prose fields are the panel's copy, not decoration. They live here rather
than in the frontend so that the words describing a feature sit next to the
stages that implement it, and a feature that grows a stage cannot quietly keep
a description that no longer matches what it does.

They are also what the *rest* of the app calls the same work. A feature is
chosen on the setup panel and then reported on by an Overview card and a
sidebar chip, and those three surfaces each used to keep a wording of their
own: "Search by description" was configured, then progressed as "Semantic
indexing", then announced as "Indexing search…". ``card_label``,
``card_running`` and ``card_icon`` are how that stopped — one name and one mark
per feature, composed here for whichever card ends up showing it.

L0: this module is a table and the functions that query it. It names stage
kinds and section ids as plain strings on purpose -- importing the pipeline to
get them would invert the layering, and ``tests/unit/test_features.py`` checks
both directions of that agreement instead.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

# Sections of the GUI that exist regardless of what an archive runs: the
# Overview reports the pipeline itself, and the Library and Timeline are what
# indexing produces, so they never depend on an optional feature.
ALWAYS_ON_SECTIONS = ("overview",)


@dataclass(frozen=True)
class Feature:
    """One thing an archive can be asked to do, and everything that follows from it."""

    id: str
    # What this feature is called, everywhere. The setup card, the chain link,
    # the Overview card reporting its progress and the sidebar chip all read
    # this one string, so the archive that chose "Search by description" is
    # never afterwards shown a card called "Semantic indexing".
    label: str
    # The mark that identifies it, on the same four surfaces. A key into the
    # frontend's ICONS table rather than the drawing itself: markup belongs in
    # the frontend, but *which* mark is part of what a feature is called.
    icon: str
    # One line, shown under the title on the card. Says what the user gets, in
    # their words -- never the name of the model or the stage.
    tagline: str
    # The Overview card's line while its stages run, kept in the two halves a
    # fused card has to recombine: "Finding" + "people" + "pets" gives
    # "Finding people & pets…", where two finished sentences could not be
    # joined. Features sharing a card must therefore share a verb, which
    # ``tests/unit/test_features.py`` checks.
    verb: str
    noun: str
    # The paragraph behind the card's "What this does". Long enough to answer
    # "should I turn this on", which means it has to be honest about cost.
    detail: str
    # Required features cannot be removed: everything else in the pipeline is
    # derived from what they produce. They are shown in the panel anyway, so the
    # setup screen describes the whole archive rather than only its options.
    required: bool
    # Pipeline stage kinds this feature owns. A stage runs when ANY of its
    # owning features is enabled -- people and pets share one fused pass.
    stages: tuple[str, ...]
    # The Overview card those stages roll up into.
    card: str
    # Nav sections this feature unlocks.
    sections: tuple[str, ...]
    # Weights fetched from upstream on first run, in MB, on a packaged build.
    # Installers bundle the two self-exported models (AdaFace, DINOv2), so a
    # source checkout downloads more than this; the number the panel shows is
    # resolved against what is actually on disk (services/archives.py).
    download_mb: int = 0
    # A feature whose accuracy depends on this one also being on. Both halves of
    # such a pair name each other, and the panel says so when only one is chosen.
    pairs_with: str = ""
    # For the two features that share the fused detect pass: which detector this
    # one turns on. The values are ``detect.results.FACE`` / ``PET``, spelled out
    # rather than imported because that module is a layer above this one;
    # ``tests/unit/test_features.py`` checks the two spellings still agree.
    detector: str = ""
    # The same idea for the fused text pass: which reader this feature turns on.
    # ``text.results.DOCUMENTS`` / ``OCR``, spelled out for the same reason and
    # checked the same way. A file's row records which of these were on when it
    # was read, so switching the other one on later brings the file back.
    extractor: str = ""


FEATURES: tuple[Feature, ...] = (
    Feature(
        id="index",
        label="Indexing",
        icon="library",
        tagline="Find every file and extract its metadata",
        verb="Scanning",
        noun="files",
        detail=(
            "Walks the folder and every folder inside it, to any depth, and records "
            "every file it finds. For each one it extracts what the file already "
            "knows about itself: its dimensions, its camera, its GPS coordinates if "
            "it has any, and above all a date, resolved from Google Takeout sidecars, "
            "embedded metadata, the filename, and finally the file's own timestamp. "
            "Each date keeps a note of where it came from, and nothing is moved, "
            "renamed or edited."
        ),
        required=True,
        stages=("scan", "enrich"),
        card="scan",
        sections=("library", "timeline"),
    ),
    Feature(
        id="duplicates",
        label="Duplicates",
        icon="dups",
        tagline="Group the copies of the same thing",
        verb="Finding",
        noun="duplicates",
        detail=(
            "Groups byte-identical copies, and photos that are the same shot re-saved by "
            "a different export or messaging app. One copy in each group is picked as the "
            "one to show and the rest are hidden from browsing, never deleted, so you can "
            "bring them back at any point."
        ),
        required=True,
        stages=("dedup",),
        card="dedup",
        sections=("dups",),
    ),
    Feature(
        id="people",
        label="People",
        icon="people",
        tagline="Group photos by who is in them",
        verb="Finding",
        noun="people",
        detail=(
            "Finds faces, checks each one is sharp, large and complete enough to trust, "
            "and groups them into people you can name, correct, merge and split. Video is "
            "covered too, from a few frames sampled per clip."
        ),
        required=False,
        stages=("detect",),
        card="detect",
        sections=("people",),
        download_mb=275,
        pairs_with="pets",
        detector="face",
    ),
    Feature(
        id="pets",
        label="Pets",
        icon="pets",
        tagline="Find the cats, dogs, birds and horses",
        verb="Finding",
        noun="pets",
        detail=(
            "Finds cats, dogs, birds and horses, then groups the ones it is confident are "
            "the same animal, so a pet gets a page of its own the way a person does."
        ),
        required=False,
        stages=("detect",),
        card="detect",
        sections=("pets",),
        download_mb=35,
        pairs_with="people",
        detector="pet",
    ),
    Feature(
        id="places",
        label="Places",
        icon="places",
        tagline="Find the places you go and put them on a map",
        verb="Mapping",
        noun="locations",
        detail=(
            "Gathers photos that already carry GPS coordinates into the places you keep "
            "going back to, so you can name them, pin them and correct them. Photos that "
            "carry no coordinates of their own can be added to a place by hand."
        ),
        required=False,
        stages=("places",),
        card="places",
        sections=("places",),
    ),
    Feature(
        id="semantic",
        label="Search by description",
        icon="semantic",
        tagline="Find a photo by describing what is in it",
        verb="Indexing",
        noun="photos for search",
        detail=(
            "Indexes every photo and video as an embedding, a fingerprint of what is "
            "actually in the frame, so “a dog on the beach” finds the shot without "
            "anything having been named or tagged."
        ),
        required=False,
        stages=("semantic",),
        card="semantic",
        sections=(),
        download_mb=689,
    ),
    Feature(
        id="documents",
        label="Documents",
        icon="documents",
        tagline="Find a document by a phrase inside it",
        verb="Reading",
        noun="documents",
        detail=(
            "Reads the text a file already carries: Word, Excel and PowerPoint, "
            "OpenDocument, plain text, Markdown, CSV, web pages, notebooks, and PDFs "
            "that store their characters rather than a picture of them. A PDF that is "
            "only pictures of a page holds nothing for this to read — that one is "
            "Pictures of text."
        ),
        required=False,
        stages=("text",),
        card="text",
        sections=(),
        # Deliberately no ``pairs_with``. Documents and Pictures of text share a
        # stage, but they are not a pair in this field's sense: People and Pets
        # check each other's work and each is more accurate for the other being
        # on, which is what the panel's note about a lonely half actually says.
        # These two read *different text* -- one the characters a file stores,
        # one the pixels -- and neither improves the other. The detail above says
        # which is which, where a note claiming they verify each other would be
        # wrong.
        extractor="documents",
    ),
    Feature(
        id="ocr",
        label="Pictures of text",
        icon="ocr",
        tagline="Read the writing in screenshots, photos and scanned PDFs",
        verb="Reading",
        noun="pictures of text",
        detail=(
            "Reads writing off the pixels: a photographed receipt, a screenshot, and "
            "above all a PDF from a scanner, where the page is an image and the file "
            "holds no text to find. Spanish and English, accents included. This is the "
            "slow one \u2014 about half a second per picture, so a hundred thousand of "
            "them is an overnight job rather than a coffee break. It stops and resumes "
            "safely at any point."
        ),
        required=False,
        stages=("text",),
        card="text",
        sections=(),
        # Nothing to download: unusually for a model here, the weights ship
        # inside the wheel (ADR 0019), so this is honest rather than optimistic.
        download_mb=0,
        extractor="ocr",
    ),
)

_BY_ID = {f.id: f for f in FEATURES}


def by_id(feature_id: str) -> Feature | None:
    """One feature by id, or None if nothing goes by that name."""
    return _BY_ID.get(feature_id)


def ids() -> tuple[str, ...]:
    """Every feature id, in the order the panel lists them."""
    return tuple(f.id for f in FEATURES)


def required_ids() -> frozenset[str]:
    """The features an archive always has, whatever it was configured with."""
    return frozenset(f.id for f in FEATURES if f.required)


def resolve(chosen: Iterable[str] | None) -> tuple[str, ...]:
    """The feature set an archive actually gets, from whatever it was given.

    ``None`` means "never configured", which is every archive added before this
    existed, and answers with the full set — an upgrade must not silently switch
    features off in an archive that has been using them.

    Anything else is treated as a *choice*: unknown ids are dropped rather than
    raising (a saved config that mentions a feature this version no longer has
    should still open), and the required ids are added back, since they are not
    the user's to decline.
    """
    if chosen is None:
        return ids()
    picked = set(chosen) | required_ids()
    return tuple(f.id for f in FEATURES if f.id in picked)


def stage_kinds(enabled: Iterable[str]) -> frozenset[str]:
    """The pipeline stage kinds this feature set may run.

    A stage owned by several features runs when any one of them is on, which is
    what lets People and Pets be chosen separately while sharing one decode.
    """
    on = set(enabled)
    return frozenset(kind for f in FEATURES if f.id in on for kind in f.stages)


def sections(enabled: Iterable[str]) -> tuple[str, ...]:
    """The GUI sections this feature set unlocks, always-on ones included."""
    on = set(enabled)
    unlocked = [s for f in FEATURES if f.id in on for s in f.sections]
    return (*ALWAYS_ON_SECTIONS, *unlocked)


def detectors(enabled: Iterable[str]) -> frozenset[str]:
    """Which detectors the fused detect pass should run for this feature set."""
    on = set(enabled)
    return frozenset(f.detector for f in FEATURES if f.id in on and f.detector)


def extractors(enabled: Iterable[str]) -> frozenset[str]:
    """Which readers the fused text pass should run for this feature set.

    The same shape as ``detectors`` and for the same reason: one stage serves two
    independently-chosen features, so it has to be told which half it is running
    rather than inferring it. Here the answer is also recorded per file, because
    a document read with only one half on may become work again when the other
    is switched on (``doc_text.wanted``).
    """
    on = set(enabled)
    return frozenset(f.extractor for f in FEATURES if f.id in on and f.extractor)


def owners(card: str) -> tuple[Feature, ...]:
    """Every feature that rolls up into one Overview card."""
    return tuple(f for f in FEATURES if f.card == card)


def _live(card: str, enabled: Iterable[str]) -> tuple[Feature, ...]:
    """The features that put work on one card and are switched on here.

    Falls back to every owner when none of them is, which keeps the three
    naming helpers total. A card with no live owner is never rendered — the
    pipeline does not build one — so this is a default, not a case.
    """
    on = set(enabled)
    return tuple(f for f in owners(card) if f.id in on) or owners(card)


def _joined(parts: Iterable[str]) -> str:
    """Sentence-case join, for the one card two features share.

    The first part keeps its capital and the rest lose theirs, so People and
    Pets read as "People & pets" rather than as two titles bolted together.
    """
    return " & ".join(p if i == 0 else p[:1].lower() + p[1:] for i, p in enumerate(parts))


def card_label(card: str, enabled: Iterable[str]) -> str:
    """What to call one Overview card: the name of the feature that put it there.

    This is deliberately the *same* string the setup panel prints on the card
    the user pressed. Those two screens used to keep separate wordings, so an
    archive was configured with "Search by description" and then reported on
    under "Semantic indexing", and nothing on either screen said they were the
    same thing.

    A card whose owning features are not all switched on is named after the
    ones that are — "People" rather than "People & pets" — because a card
    naming work the archive was never asked to do is a card the user cannot act
    on.
    """
    return _joined(f.label for f in _live(card, enabled))


def card_running(card: str, enabled: Iterable[str]) -> str:
    """What one Overview card says while its stages are actually running.

    Composed rather than stored so that the fused detect card reports the half
    it is running: an archive that asked only for Pets gets "Finding pets…",
    where the fixed string it used to show promised people it was never going
    to look for.
    """
    live = _live(card, enabled)
    return f"{live[0].verb} {_joined(f.noun for f in live)}…"


def card_always_runs(card: str) -> bool:
    """Whether every feature feeding this card is one an archive cannot decline.

    Independent of what any archive enabled, which is the point: it is the
    difference between the trunk of the pipeline and what clips onto it. The
    setup panel states it in words ("Indexing and Duplicates always run. Every
    other stage reads what they produce") and the Overview draws it on its
    rail, so where a card sits in the chain is visible where the work is and
    not only where it was chosen.
    """
    return all(f.required for f in owners(card))


def card_icon(card: str, enabled: Iterable[str]) -> str:
    """The mark one Overview card carries, keyed into the frontend's ICONS.

    A fused card takes the mark of whichever half is listed first — the same
    half whose label opens its title, so the name and the mark never point at
    different features.
    """
    return _live(card, enabled)[0].icon


# --- What Browse's one search box can be asked --------------------------------


@dataclass(frozen=True)
class SearchWay:
    """One way a typed query can be answered, as Browse presents it.

    A *way* is not a feature and there are fewer of them than there are
    features. Four readers fill indexes -- a file's name, a photo, a document's
    text layer, writing read off pixels -- but only three rankings can answer a
    query, because Documents and Pictures of text both feed one of them (ADR
    0020). So this composes the three, and ``readers`` is what says which
    features got you each one.

    It lives here for the same reason ``card_label`` does. Browse is the fourth
    surface to name this work, after the setup panel, the Overview card and the
    sidebar chip, and it briefly grew a wording of its own -- "What your photos
    show" for the thing every other screen calls Search by description. That is
    the drift this module exists to prevent, so the words come from the same
    table as the rest.
    """

    id: str
    # What the group of results is headed with, and the row in the panel that
    # promises it. The feature's own label wherever one feature owns the way.
    label: str
    icon: str
    # One line under the label, in the reader's terms rather than the
    # catalogue's: the label says what you switched on, this says what it does
    # to what you type.
    matches: str
    # File names are not a feature and nobody chose them, so the panel marks
    # this one as always present rather than leaving it looking declinable.
    always: bool
    # The features feeding this way, in catalogue order. What the panel draws a
    # documentation link per, and empty for the way no feature owns.
    readers: tuple[str, ...]


# The way that belongs to no feature. Indexing records every file's name, but
# heading a group of results "Indexing" would name the stage rather than the
# answer -- and unlike the other two there is nothing here anybody chose, so
# there is no feature label to keep faith with.
#
# Which leaves this one name free, and it is phrased the way the other two read
# rather than as the thing it matches: the panel lists ways of searching and the
# headings say how a result was found, so "Search by filename" belongs beside
# "Search by description" in a way that a bare "File names" did not.
_NAME_WAY = SearchWay(
    id="name",
    label="Search by filename",
    icon="filename",
    matches="Matches the words in a file's own name, in any order.",
    always=True,
    readers=(),
)


def _text_matches(on: set[str]) -> str:
    """What the text way promises, from the readers actually switched on.

    Composed rather than stored because "the words inside your files" means a
    different set of files depending on which halves are running, and an archive
    that reads only pictures must not be promised its documents.
    """
    reads = (
        "the words inside your documents, and the writing in your pictures"
        if {"documents", "ocr"} <= on
        else "the writing in your screenshots, photos and scanned PDFs"
        if "ocr" in on
        else "the words written inside your documents"
    )
    return f"Matches {reads}."


def search_ways(enabled: Iterable[str]) -> tuple[SearchWay, ...]:
    """The ways this feature set can answer a typed query, in the order shown.

    Ordered by how explainable an answer is, which is the rule the results
    already followed for putting text above the photo grid: a name match is the
    most literal thing Browse can show you, a passage carrying your word is
    next, and a picture that merely looks like what you described is last.
    """
    on = set(enabled)
    ways = [_NAME_WAY]
    # Documents and Pictures of text share the `text` card, so its label and mark
    # already compose the way either one alone -- or both -- should be called.
    # They are one way rather than two because they write into the *same*
    # passages and the same index: a hit's reader is a property of the file's
    # ``doc_text`` row, not of a separate search (ADR 0020).
    text_readers = tuple(f.id for f in owners("text") if f.id in on)
    if text_readers:
        ways.append(
            SearchWay(
                id="text",
                label=card_label("text", on),
                icon=card_icon("text", on),
                matches=_text_matches(on),
                always=False,
                readers=text_readers,
            )
        )
    if "semantic" in on:
        semantic = _BY_ID["semantic"]
        ways.append(
            SearchWay(
                id="media",
                label=semantic.label,
                icon=semantic.icon,
                matches="Matches what is in the frame, without anything having been tagged.",
                always=False,
                readers=("semantic",),
            )
        )
    return tuple(ways)
