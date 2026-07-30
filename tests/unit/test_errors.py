"""The error vocabulary's one load-bearing property.

The HTTP layer will branch on `except TroveError` to decide between "show this
message to the user" and "log a traceback and return 500". That only works if
every deliberate error really does descend from TroveError, and if TroveError
descends from Exception rather than BaseException -- the existing broad handlers
around model loading must keep catching these, or a missing optional extra
would go from a reported condition to a crashed job.
"""

from __future__ import annotations

import pytest

from organize_archive import errors


@pytest.mark.parametrize(
    "cls",
    [
        errors.ConfigError,
        errors.MissingToolError,
        errors.ModelUnavailableError,
        errors.ArchiveError,
    ],
)
def test_every_deliberate_error_is_a_trove_error(cls):
    assert issubclass(cls, errors.TroveError)
    assert issubclass(cls, Exception)


def test_a_model_failure_is_still_caught_by_the_existing_broad_handlers():
    # detect.make_backends wraps backend construction in `except Exception` so a
    # missing detector reports and carries on with the other one.
    try:
        raise errors.ModelUnavailableError("no model")
    except Exception as exc:
        assert isinstance(exc, errors.TroveError)
