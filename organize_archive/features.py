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
    label: str
    # One line, shown under the title on the card. Says what the user gets, in
    # their words -- never the name of the model or the stage.
    tagline: str
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
        tagline="Find every file and work out when it was taken",
        detail=(
            "Walks the folder and records every photo, video, audio file and document "
            "it finds, then resolves a date for each one from Google Takeout sidecars, "
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
        tagline="Group the copies of the same thing",
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
        tagline="Group photos by who is in them",
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
        tagline="Find the cats, dogs, birds and horses",
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
        tagline="Find the places you go and put them on a map",
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
        tagline="Find a photo by describing what is in it",
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


def card_label(card: str, enabled: Iterable[str], default: str) -> str:
    """What to call a card whose owning features are not all switched on.

    The detect card reads "People & pets" when both are on, and simply "People"
    (or "Pets") when one of them is not — a card naming work the archive was
    never asked to do is a card the user cannot act on.
    """
    on = [f for f in owners(card) if f.id in set(enabled)]
    if len(on) == 1 and len(owners(card)) > 1:
        return on[0].label
    return default
