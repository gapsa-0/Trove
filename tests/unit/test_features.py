"""The feature catalogue, and the four agreements it has to keep.

``organize_archive/features.py`` is a table that three other modules read, and
it names things that live above it as plain strings -- stage kinds, detector
names, section ids -- because importing them would invert the layering. Each of
those spellings is checked here instead, so a rename in the pipeline that leaves
the catalogue behind fails a test rather than silently switching a feature off.
"""

from __future__ import annotations

import pytest

from organize_archive import features
from organize_archive.detect import results as detect_results
from organize_archive.pipeline import stages


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
    both = features.card_label("detect", ["people", "pets"], "People & pets")
    assert both == "People & pets"
    assert features.card_label("detect", ["people"], "People & pets") == "People"
    assert features.card_label("detect", ["pets"], "People & pets") == "Pets"
    # A card with a single owner keeps its own name whatever is passed.
    assert features.card_label("places", ["places"], "Location mapping") == "Location mapping"


def test_the_panel_copy_is_actually_there():
    """These strings are the whole user-facing explanation of a decision that
    can cost 689 MB, and they are the reason the catalogue lives in Python
    rather than in the frontend."""
    for f in features.FEATURES:
        assert f.label and f.tagline and f.detail
        assert len(f.detail) > 80, f.id
        assert not f.tagline.endswith("."), f.id
