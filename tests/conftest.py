import pytest


@pytest.fixture(autouse=True)
def isolate_app_data(monkeypatch, tmp_path_factory):
    """No test may resolve to the user's real archive store.

    Several tests build a JobManager, whose scheduler thread keeps ticking
    after the test that made it returns. A tick reads the archive registry and
    is allowed to start real work, so a stray one must never be able to find a
    genuine archive to scan.
    """
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path_factory.mktemp("appdata")))
