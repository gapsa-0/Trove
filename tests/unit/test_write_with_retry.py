"""`db.write_with_retry`'s contract: it returns what the write returned.

Every caller in the app takes the default `retries`, so the interesting cases
here are the ones no caller exercises -- which is exactly why they are worth
pinning: a helper that can silently skip a write is a data-loss bug waiting
for its first caller.
"""

import sqlite3

import pytest

from trove.db import database as db


def test_returns_the_writes_result():
    assert db.write_with_retry(lambda: {"ok": True}) == {"ok": True}


def test_retries_a_locked_database_then_returns():
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise sqlite3.OperationalError("database is locked")
        return "written"

    assert db.write_with_retry(flaky, initial_delay=0) == "written"
    assert len(attempts) == 3


def test_gives_up_after_the_last_retry():
    def always_locked():
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        db.write_with_retry(always_locked, retries=1, initial_delay=0)


def test_an_error_that_is_not_a_lock_is_not_retried():
    attempts = []

    def broken():
        attempts.append(1)
        raise sqlite3.OperationalError("no such table: files")

    with pytest.raises(sqlite3.OperationalError):
        db.write_with_retry(broken, initial_delay=0)
    assert len(attempts) == 1


def test_a_negative_retry_count_is_refused_rather_than_skipping_the_write():
    # It used to return None having called `fn` zero times: the write silently
    # did not happen and the caller could not tell.
    called = []
    with pytest.raises(ValueError, match="retries must be >= 0"):
        db.write_with_retry(lambda: called.append(1), retries=-1)
    assert called == []
