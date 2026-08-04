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
