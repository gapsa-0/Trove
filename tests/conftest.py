import factories
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


# The obvious name for the next fixture is `db`, and it must not be: 28 test
# modules do `from trove.db import database as db`, so a fixture
# argument called `db` would shadow that import inside exactly the tests that
# request it. `conn` is out for the same reason -- it is the local name for a
# connection almost everywhere.
@pytest.fixture
def catalog(tmp_path):
    """An initialised catalogue with one root (id 1) under tmp_path.

    The connection is left open; sqlite closes it when it is collected, and a
    test that wants to prove something about closing should do it explicitly.
    """
    return factories.make_db(tmp_path)


@pytest.fixture
def source_root(tmp_path):
    """An empty fake archive root. Pass files via factories.make_archive instead
    when the test needs content."""
    return factories.make_archive(tmp_path)
