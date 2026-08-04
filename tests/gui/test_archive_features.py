"""Choosing an archive's features, and the gate that choice operates.

The promise this file protects is the one the setup screen makes: a feature you
did not ask for is not merely hidden, it never runs.

The gate is one thing in one place: ``stage_states`` leaves a disabled stage out
of the list, and the scheduler starts what that list says is queued. So testing
the list is testing the gate.

The other half of that promise — that a feature you did not ask for never
downloads its models either — is held by ``test_model_fetch.py``, since the
weights are fetched ahead of the stage that needs them and no longer by it.
"""

from __future__ import annotations

import pytest

from organize_archive import features
from organize_archive.config import Config
from organize_archive.pipeline import stages, status
from organize_archive.services import archives as archive_service


class _Jobs:
    """The bit of JobManager stage_states reads, and nothing else."""

    def __init__(self, cfg):
        self.cfg = cfg

    def disk_count(self, *_a, **_k):
        return None

    def dedup_needed(self, *_a):
        return False

    def list(self, *_a):
        return []

    def paused(self):
        return False

    def paused_stages(self):
        return frozenset()


@pytest.fixture
def archive(tmp_path):
    """A registered archive, added exactly the way the API adds one.

    ``XDG_DATA_HOME`` already points somewhere throwaway (tests/conftest.py's
    autouse fixture), so this writes to a scratch app-data dir, not a real one.
    """
    source = tmp_path / "src"
    source.mkdir()
    cfg = Config(db_path=str(tmp_path / "legacy.db"), cache_dir=str(tmp_path / "cache"))
    added = archive_service.add_archive(cfg, str(source))
    assert "error" not in added, added
    return cfg, added["id"]


def _kinds(cfg, aid):
    return {s["kind"] for s in stages.stage_states(cfg, _Jobs(cfg), aid, "/nope")}


def test_an_archive_that_was_never_configured_runs_everything(archive):
    cfg, aid = archive
    assert _kinds(cfg, aid) == {sd.kind for sd in stages.STAGES}


def test_a_disabled_stage_is_absent_rather_than_reported_as_off(archive):
    """Absent, not "unavailable": the scheduler starts whatever the list calls
    queued, so a stage that is not in the list can never be started, and no
    special case in the scheduler is needed to keep it that way."""
    cfg, aid = archive
    cfg.set_archive_features(aid, ["places"])
    assert _kinds(cfg, aid) == {"scan", "enrich", "dedup", "places"}


def test_the_minimum_archive_still_scans_and_dedups(archive):
    cfg, aid = archive
    cfg.set_archive_features(aid, [])
    assert _kinds(cfg, aid) == {"scan", "enrich", "dedup"}


def test_a_feature_switched_on_later_brings_its_stage_back(archive):
    """Nothing is destroyed by switching a feature off, so switching it on again
    is just the stage reappearing with whatever backlog it still owes."""
    cfg, aid = archive
    cfg.set_archive_features(aid, [])
    assert "places" not in _kinds(cfg, aid)
    cfg.set_archive_features(aid, ["places"])
    assert "places" in _kinds(cfg, aid)


def test_the_cards_lose_the_features_the_archive_does_not_run(archive):
    cfg, aid = archive
    cfg.set_archive_features(aid, ["people"])
    snapshot = status.snapshot(cfg, _Jobs(cfg), aid, "/nope")
    shown = {c["id"] for c in snapshot["stages"]}
    assert shown == {"scan", "dedup", "detect"}
    assert "places" not in shown and "semantic" not in shown


def test_the_shared_card_is_renamed_when_only_one_half_is_on(archive):
    cfg, aid = archive
    cfg.set_archive_features(aid, ["people"])
    card = next(
        c for c in status.snapshot(cfg, _Jobs(cfg), aid, "/nope")["stages"] if c["id"] == "detect"
    )
    assert card["label"] == "People"


def test_people_and_pets_are_chosen_separately_but_share_one_stage(archive):
    cfg, aid = archive
    cfg.set_archive_features(aid, ["people", "pets"])
    detect_stages = [
        s for s in stages.stage_states(cfg, _Jobs(cfg), aid, "/nope") if s["card"] == "detect"
    ]
    assert len(detect_stages) == 1, "ADR 0004: one fused pass, however many features want it"


def test_the_registry_keeps_the_choice_and_the_name(archive):
    cfg, aid = archive
    assert cfg.archive_name(aid) == "src"
    cfg.set_archive_name(aid, "  Family photos  ")
    assert cfg.archive_name(aid) == "Family photos"
    # Clearing it hands the archive back to its folder rather than storing "".
    cfg.set_archive_name(aid, "")
    assert cfg.archive_name(aid) == "src"


def test_a_choice_survives_a_reload_of_the_config(archive, tmp_path):
    cfg, aid = archive
    cfg.set_archive_features(aid, ["semantic"])
    cfg.save()
    reloaded = Config.load()
    assert reloaded.archive_features(aid) == ("index", "duplicates", "semantic")


def test_detection_reports_unavailable_only_for_the_detectors_asked_for(archive, monkeypatch):
    """An importable face backend does not make the stage available to an
    archive that only asked for Pets -- it would report itself ready and then
    fail on its first run."""
    monkeypatch.setattr("organize_archive.faces.backend.available", lambda: True)
    monkeypatch.setattr("organize_archive.pets.backend.available", lambda: False)
    cfg, aid = archive
    cfg.set_archive_features(aid, ["pets"])
    avail = stages._availability(cfg, cfg.archive_features(aid))
    assert avail[stages.DETECT] is False
    cfg.set_archive_features(aid, ["people"])
    assert stages._availability(cfg, cfg.archive_features(aid))[stages.DETECT] is True


def test_the_catalogue_reports_what_this_installation_can_offer(archive):
    cfg, _aid = archive
    catalog = {f["id"]: f for f in archive_service.features(cfg)}
    assert set(catalog) == set(features.ids())
    assert catalog["index"]["required"] and catalog["index"]["available"]
    # Nothing to fetch means nothing to wait for, whatever is installed.
    assert catalog["places"]["download_mb"] == 0 and catalog["places"]["ready"] is True
