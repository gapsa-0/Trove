"""Whether a folder can become an archive, asked before one is configured.

The picker used to find out at the end: you chose a folder, picked eight
features, pressed Create, and only then heard that the folder was already an
archive. Both refusals are true the moment the folder is chosen and have
nothing to do with what was configured, so they are asked there -- through the
same function the creation path runs, which is what these hold to.
"""

from trove.config import Config
from trove.services.archives import add_archive, check_folder


def _config(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return Config.load()


def test_a_fresh_folder_can_become_an_archive(monkeypatch, tmp_path):
    cfg = _config(monkeypatch, tmp_path)
    folder = tmp_path / "photos"
    folder.mkdir()

    answer = check_folder(cfg, str(folder))

    assert answer["ok"] is True
    # The resolved path, which is what "the same folder" means here.
    assert answer["path"] == str(folder.resolve())


def test_a_folder_that_is_already_an_archive_says_which_one(monkeypatch, tmp_path):
    """Naming the archive is the useful half: at that point what you want to
    know is which of the folders on the start page this one is."""
    cfg = _config(monkeypatch, tmp_path)
    folder = tmp_path / "photos"
    folder.mkdir()
    added = add_archive(cfg, str(folder))

    answer = check_folder(cfg, str(folder))

    assert answer["error"] == "That folder is already an archive."
    assert answer["archive_id"] == added["id"]


def test_a_path_that_is_not_a_folder_is_refused(monkeypatch, tmp_path):
    cfg = _config(monkeypatch, tmp_path)
    loose = tmp_path / "notes.txt"
    loose.write_text("not a folder")

    assert "Not a directory" in check_folder(cfg, str(loose))["error"]


def test_the_same_folder_reached_by_another_name_is_still_that_folder(monkeypatch, tmp_path):
    """Why the server answers this and the start page does not compare strings:
    a trailing slash, a `..`, or a symlink is the same folder under another
    name, and letting one of those through registers it twice."""
    cfg = _config(monkeypatch, tmp_path)
    folder = tmp_path / "photos"
    folder.mkdir()
    add_archive(cfg, str(folder))

    for other in (f"{folder}/", str(tmp_path / "photos" / "." / ""), str(folder / ".." / "photos")):
        assert "already an archive" in check_folder(cfg, other)["error"], other


def test_creating_an_archive_runs_the_same_check(monkeypatch, tmp_path):
    """The two must not be able to disagree: a folder the picker accepted and
    the creation refused would send someone through setup for nothing."""
    cfg = _config(monkeypatch, tmp_path)
    folder = tmp_path / "photos"
    folder.mkdir()
    add_archive(cfg, str(folder))

    assert add_archive(cfg, str(folder))["error"] == check_folder(cfg, str(folder))["error"]
