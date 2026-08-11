"""What ``Request`` does with the two query values every root-scoped route reads.

Both of these are answers to a hand-edited or stale URL rather than to anything
the frontend sends, which is exactly why they are worth pinning: nothing in the
app exercises them, so a regression here is invisible until a user meets it.
"""

from __future__ import annotations

import pytest

from trove.config import Config
from trove.errors import ArchiveError
from trove.web.routes._request import Request


def _request(query: dict[str, list[str]], archives: list[dict] | None = None) -> Request:
    cfg = Config()
    cfg.archives = (
        archives
        if archives is not None
        else [{"id": 1, "path": "/photos", "added_at": "2026-01-01"}]
    )
    return Request(
        method="GET",
        path="/api/dups",
        query=query,
        body={},
        cfg=cfg,
        jobs=None,  # type: ignore[arg-type]
    )


class TestLimit:
    """``?limit=`` is defaulted, then clamped at BOTH ends."""

    def test_a_missing_limit_takes_the_route_default(self):
        assert _request({}).limit(60, 200) == 60

    def test_a_limit_over_the_cap_is_cut_to_it(self):
        assert _request({"limit": ["99999"]}).limit(60, 200) == 200

    @pytest.mark.parametrize("value", ["-1", "-5", "0"])
    def test_a_limit_below_one_is_raised_to_one(self, value):
        """The regression this exists for.

        Only the top used to be clamped, and SQLite reads a negative ``LIMIT``
        as no limit at all -- so ``?limit=-1`` reached the query as ``LIMIT -1``
        and returned every row in the table, which is the one thing the cap is
        there to refuse. Zero is included because a page of nothing is a request
        the caller cannot have meant either.
        """
        assert _request({"limit": [value]}).limit(60, 200) == 1


class TestArchiveResolution:
    """An id has to name an archive that is actually registered."""

    def test_a_registered_archive_resolves_to_its_own_database(self):
        assert _request({"root": ["1"]}).db(1).endswith("archive.db")

    def test_a_missing_root_says_so(self):
        with pytest.raises(ValueError, match="root is required"):
            _request({}).db(None)

    def test_an_unregistered_archive_is_named_in_the_error(self):
        """The regression this exists for.

        The id used to go straight to ``archive_db_path``, which names a
        database file whether or not it was ever created; the caller then got
        SQLite's own words back -- ``unable to open database file`` -- which
        says nothing about archives and reads like a broken disk rather than a
        request for something that is not there. It is a real path: a bookmark
        into an archive that has since been removed, or a tab left open while
        another window deleted it.
        """
        with pytest.raises(ArchiveError, match="there is no archive 999"):
            _request({"root": ["999"]}).db(999)

    def test_the_cache_directory_is_scoped_the_same_way(self):
        """Both resolvers share the check, so neither can drift from the other."""
        with pytest.raises(ArchiveError, match="there is no archive 999"):
            _request({"root": ["999"]}).cache(999)
