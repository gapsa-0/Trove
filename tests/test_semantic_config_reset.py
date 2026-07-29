"""A config.json written before local embeddings must not poison the new one.

The catalogue is rebuilt from scratch when the embedder changes, but
``config.json`` is not: ``Config.load`` copies every key it finds onto the
dataclass, so a saved value shadows a changed default forever. Left alone, an
upgraded install would write 768-d SigLIP vectors while recording
``dimensions: 1024``, and filter them at 0.25 -- a threshold from a completely
different similarity scale, which returns an empty grid for every query and
looks exactly like "the local model is bad".
"""

from __future__ import annotations

import json

from organize_archive.config import Config, discard_superseded_secrets
from organize_archive.paths import config_file, secrets_file


def _write_config(data):
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_a_voyage_era_config_loads_as_the_local_defaults():
    _write_config({
        "semantic_embedding_model": "voyage-multimodal-3.5",
        "semantic_embedding_dimensions": 1024,
        "semantic_search_min_similarity": 0.25,
        "semantic_inline_max_bytes": 20971520,
        "timezone": "America/Argentina/Buenos_Aires",
    })

    cfg = Config.load()
    fresh = Config()

    assert cfg.semantic_embedding_model == fresh.semantic_embedding_model
    assert cfg.semantic_embedding_dimensions == fresh.semantic_embedding_dimensions
    assert cfg.semantic_search_min_similarity == fresh.semantic_search_min_similarity
    # Everything unrelated is still honoured -- this resets three fields, not
    # the user's settings.
    assert cfg.timezone == "America/Argentina/Buenos_Aires"


def test_the_reset_is_written_back_so_it_happens_once():
    path = _write_config({
        "semantic_embedding_model": "voyage-multimodal-3.5",
        "semantic_embedding_dimensions": 1024,
        "semantic_search_min_similarity": 0.25,
        "semantic_inline_max_bytes": 20971520,
    })

    Config.load()

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["semantic_embedding_model"] == Config().semantic_embedding_model
    assert saved["semantic_embedding_dimensions"] == 768
    # The retired field is gone from disk, not merely ignored in memory.
    assert "semantic_inline_max_bytes" not in saved


def test_a_deliberately_tuned_local_threshold_survives_a_reload():
    """The reset must not become a permanent override.

    It fires only for a config that still names the *old* model; once the file
    records the local one, a user-tuned threshold is theirs to keep.
    """
    _write_config({
        "semantic_embedding_model": Config().semantic_embedding_model,
        "semantic_embedding_dimensions": 768,
        "semantic_search_min_similarity": 0.11,
    })

    assert Config.load().semantic_search_min_similarity == 0.11


def test_the_retired_voyage_key_is_deleted_from_disk():
    """A credential for a feature that no longer exists must not be left lying
    around: nothing in the app can spend it, and it is still a live secret."""
    path = secrets_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"voyage_api_key": "pa-secret"}), encoding="utf-8")

    discard_superseded_secrets()

    # The file held nothing else, so it goes too.
    assert not path.exists()


def test_other_secrets_are_preserved_when_the_voyage_key_is_removed():
    path = secrets_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"voyage_api_key": "pa-secret", "something_else": "keep"}),
        encoding="utf-8")

    discard_superseded_secrets()

    remaining = json.loads(path.read_text(encoding="utf-8"))
    assert remaining == {"something_else": "keep"}


def test_discarding_secrets_is_safe_when_there_is_no_secrets_file():
    assert not secrets_file().exists()
    discard_superseded_secrets()          # must not raise
