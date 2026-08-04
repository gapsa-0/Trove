"""The feature catalogue, and the four agreements it has to keep.

``trove/features.py`` is a table that three other modules read, and
it names things that live above it as plain strings -- stage kinds, detector
names, section ids -- because importing them would invert the layering. Each of
those spellings is checked here instead, so a rename in the pipeline that leaves
the catalogue behind fails a test rather than silently switching a feature off.
"""

from __future__ import annotations

import pytest

from trove import features
from trove.detect import results as detect_results
from trove.pipeline import stages


def test_every_stage_belongs_to_exactly_one_card_and_some_feature():
    """A stage no feature claims can never run; one two features claim runs when
    either is on, which is only correct for the fused detect pass."""
    claimed = {kind for f in features.FEATURES for kind in f.stages}
    assert claimed == {sd.kind for sd in stages.STAGES}
    for sd in stages.STAGES:
        owners = [f for f in features.FEATURES if sd.kind in f.stages]
        assert owners, f"{sd.kind} belongs to no feature"
        assert all(f.card == sd.card for f in owners), sd.kind
        if len(owners) > 1:
            assert sd.kind == stages.DETECT, "only the fused pass may have two owners"


def test_every_stage_depends_only_on_required_features():
    """The gate drops a disabled stage from the list entirely, so a stage whose
    dependency could be switched off would wait for a state that never arrives.
    Keeping every dependency inside the required features is what makes that
    impossible rather than merely unlikely."""
    always_on = features.stage_kinds(features.required_ids())
    for sd in stages.STAGES:
        assert set(sd.deps) <= always_on, f"{sd.kind} depends on an optional stage"


def test_detector_names_match_the_ones_detect_actually_uses():
    named = {f.detector for f in features.FEATURES if f.detector}
    assert named == set(detect_results.BOTH_DETECTORS)
    assert features.detectors(["people"]) == frozenset({detect_results.FACE})
    assert features.detectors(["pets"]) == frozenset({detect_results.PET})
    assert features.detectors(features.ids()) == detect_results.BOTH_DETECTORS


def test_the_feature_ids_spelled_out_elsewhere_still_exist():
    """Two consumers name a feature by literal id rather than importing one:
    ``web/routes/search.py`` asks whether this archive runs "semantic" before
    reporting the description index as configured, and ``static/js/state.js``
    gates the Browse composer on the same string. Renaming the feature without
    them would quietly re-open the bug they close — the composer offered on an
    archive whose semantic stage never runs."""
    assert features.by_id("semantic") is not None
    assert "semantic" in features.ids()


def test_an_unconfigured_archive_gets_everything():
    """None is what an archive added before this existed looks like. Answering
    with anything less would switch off work it has been doing for months."""
    assert features.resolve(None) == features.ids()


@pytest.mark.parametrize(
    "chosen, expected",
    [
        ([], ("index", "duplicates")),
        (["places"], ("index", "duplicates", "places")),
        (["duplicates"], ("index", "duplicates")),
        (["semantic", "nonsense-feature"], ("index", "duplicates", "semantic")),
        (["places", "index"], ("index", "duplicates", "places")),
    ],
)
def test_a_choice_keeps_the_required_and_drops_the_unknown(chosen, expected):
    assert features.resolve(chosen) == expected


def test_stage_kinds_follow_the_chosen_features():
    assert features.stage_kinds(features.resolve([])) == frozenset({"scan", "enrich", "dedup"})
    assert stages.DETECT in features.stage_kinds(features.resolve(["pets"]))
    assert stages.PLACES not in features.stage_kinds(features.resolve(["pets"]))


def test_sections_always_include_the_overview():
    """The Overview reports the pipeline itself, so it survives every choice --
    including the minimum one, which is what an archive that only wants
    duplicates removed ends up with."""
    minimal = features.sections(features.resolve([]))
    assert "overview" in minimal
    assert "people" not in minimal
    assert "dups" in minimal


def test_the_shared_card_is_named_after_whichever_half_is_on():
    """A card offering "People & pets" on an archive that never asked for pets
    describes work that will not happen."""
    assert features.card_label("detect", ["people", "pets"]) == "People & pets"
    assert features.card_label("detect", ["people"]) == "People"
    assert features.card_label("detect", ["pets"]) == "Pets"
    # A card with a single owner is simply named after it.
    assert features.card_label("places", ["places"]) == "Places"


def test_a_card_is_called_what_the_setup_panel_called_it():
    """The disconnect this table exists to close: every Overview card's name is
    the label on the setup card that switched it on, not a second wording."""
    for f in features.FEATURES:
        assert f.label in features.card_label(f.card, [f.id])
    assert features.card_label("semantic", ["semantic"]) == "Search by description"


def test_the_running_line_names_only_the_work_the_archive_asked_for():
    """The fused card used to say "Detecting people & pets…" whatever it was
    running, which on a pets-only archive promised people it would never look
    for."""
    assert features.card_running("detect", ["people", "pets"]) == "Finding people & pets…"
    assert features.card_running("detect", ["pets"]) == "Finding pets…"
    assert features.card_running("scan", ["index"]) == "Scanning files…"


def test_features_sharing_a_card_share_a_verb():
    """``card_running`` composes one verb with several nouns, so two features on
    one card disagreeing about the verb would silently drop one of them."""
    for card in {f.card for f in features.FEATURES}:
        verbs = {f.verb for f in features.owners(card)}
        assert len(verbs) == 1, card


def test_the_trunk_is_exactly_the_cards_every_archive_runs():
    """What the Overview's rail draws as filled nodes, and the setup panel as
    fixed chips. It has to agree with the gate: a card marked as always running
    on an archive that can switch it off would draw a chain link that is not
    there."""
    trunk = {c for c in stages.CARD_ORDER if features.card_always_runs(c)}
    minimal = features.stage_kinds(features.resolve([]))
    assert trunk == {stages.card_of(k) for k in minimal}
    assert trunk == {"scan", "dedup"}


def test_every_branch_card_reads_from_the_trunk():
    """The rail forks off the trunk rather than continuing through it, which is
    only honest while no optional stage depends on another optional stage."""
    for sd in stages.STAGES:
        if features.card_always_runs(sd.card):
            continue
        assert all(features.card_always_runs(stages.card_of(d)) for d in sd.deps), sd.kind


def test_the_naming_helpers_answer_for_a_card_with_nothing_enabled():
    """They are called while a card is being built, and nothing there is
    obliged to prove the card has a live owner first."""
    for card in {f.card for f in features.FEATURES}:
        assert features.card_label(card, [])
        assert features.card_running(card, [])
        assert features.card_icon(card, [])


def test_the_overview_renders_its_cards_in_the_order_the_panel_offers_them():
    """The setup chain and the Overview grid are two drawings of one pipeline.
    Ordering them from separate lists is how they would come to disagree."""
    from_catalogue = tuple(dict.fromkeys(f.card for f in features.FEATURES))
    assert from_catalogue == stages.CARD_ORDER


def test_the_panel_copy_is_actually_there():
    """These strings are the whole user-facing explanation of a decision that
    can cost 689 MB, and they are the reason the catalogue lives in Python
    rather than in the frontend."""
    for f in features.FEATURES:
        assert f.label and f.tagline and f.detail
        assert len(f.detail) > 80, f.id
        assert not f.tagline.endswith("."), f.id
        # The running line is composed, so the halves have to stay halves: a
        # verb that has swallowed its object composes into nonsense on the one
        # card that joins two of them.
        assert f.verb and f.noun and f.icon, f.id
        assert " " not in f.verb, f.id
        assert f.noun[:1].islower(), f.id
