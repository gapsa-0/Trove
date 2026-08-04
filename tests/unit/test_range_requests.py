"""What a ``Range`` header resolves to, before any file is opened.

``_parse_range`` is the half of range serving that is easy to get quietly
wrong, so it is a pure function and this is a table. Two of the cases below
are regressions rather than hypotheticals -- both shipped, and neither could
be seen from the outside without looking at the bytes that came back:

* a *suffix* range (``bytes=-20``, the LAST 20 bytes) was read as ``0-20`` and
  answered with the head of the file under a ``Content-Range`` that said so;
* a start past the end produced a negative length, sent as ``Content-Length:
  -4900`` with a 206 beside it.

The satisfiability rule is asserted here too, because it is what the caller
tests (``start >= size``) rather than something the parser folds into None:
one comparison has to cover suffix and explicit ranges alike.
"""

from __future__ import annotations

import pytest

from trove.web.server import _parse_range

_SIZE = 100


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        # -- ordinary ranges, inclusive both ends ---------------------------
        ("bytes=0-9", (0, 9)),
        ("bytes=0-0", (0, 0)),
        ("bytes=10-19", (10, 19)),
        ("bytes=0-99", (0, 99)),
        # An open end means "to the end of the file", not "to byte zero".
        ("bytes=50-", (50, 99)),
        ("bytes=0-", (0, 99)),
        # A last-pos past the end is clamped rather than refused: the client
        # asked for more than exists, and the rest of it exists.
        ("bytes=90-100000", (90, 99)),
        # -- suffix ranges: the LAST n bytes --------------------------------
        ("bytes=-20", (80, 99)),
        ("bytes=-1", (99, 99)),
        ("bytes=-100", (0, 99)),
        # More tail than there is file: the whole file is the answer.
        ("bytes=-500", (0, 99)),
        # Zero bytes of tail names nothing, and lands on `size` so that the
        # caller's one satisfiability test reports it.
        ("bytes=-0", (100, 99)),
        # -- unsatisfiable, but parsed: the caller answers 416 --------------
        ("bytes=100-", (100, 99)),
        ("bytes=5000-", (5000, 99)),
        # -- not a range this server honours: ignore it, serve the whole body
        ("items=0-9", None),
        ("bytes=0-1,5-6", None),  # a list; refused rather than half-answered
        ("bytes=50-20", None),  # backwards
        ("bytes=-", None),
        ("bytes=", None),
        ("", None),
        ("garbage", None),
        ("bytes=abc-def", None),
    ],
)
def test_a_range_header_resolves_to_an_inclusive_pair(header, expected):
    assert _parse_range(header, _SIZE) == expected


@pytest.mark.parametrize("header", ["bytes=0-", "bytes=0-0", "bytes=-1", "bytes=-500"])
def test_every_range_against_an_empty_file_is_unsatisfiable(header):
    """A zero-length file has no byte to name, whichever way it is asked for.

    Pinned because the caller's test is ``start >= size``, and with size 0 that
    only holds if the parser resolves each of these to a start of 0 -- which is
    the same arithmetic as every other case, not a special one.
    """
    start, end = _parse_range(header, 0)
    assert start >= 0 and end == -1


@pytest.mark.parametrize(
    ("header", "size"),
    [("bytes=100-", 100), ("bytes=5000-", 100), ("bytes=-0", 100), ("bytes=0-", 0)],
)
def test_the_caller_can_spot_an_unsatisfiable_range_with_one_comparison(header, size):
    start, _end = _parse_range(header, size)
    assert start >= size


@pytest.mark.parametrize(
    ("header", "size"),
    [("bytes=0-9", 100), ("bytes=-20", 100), ("bytes=99-", 100), ("bytes=-500", 100)],
)
def test_a_satisfiable_range_never_yields_a_negative_length(header, size):
    """The property the ``Content-Length: -4900`` bug violated.

    Asserted as a property rather than per-case because it is what makes the
    response well-formed at all, and it must hold for every pair the caller
    goes on to serve.
    """
    start, end = _parse_range(header, size)
    assert end - start + 1 > 0
