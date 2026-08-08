"""The inline-handler guard must not count render-time calls.

``tools/dev/check_handlers.py`` compares the functions inline ``on*`` attributes
call against the block ``main.js`` puts on ``window``. Getting *that* wrong is
the failure it exists to catch: a button that renders perfectly and does nothing.

Getting it wrong in the other direction is what this file pins. The screens build
markup inside template literals, so an attribute can hold two different moments
at once -- ``onclick="openDocs('${esc(reader.docs)}')"`` calls ``esc`` while the
string is being built, in a module where it is imported normally, and only
``openDocs`` against ``window`` when the button is clicked. Counting both made
the check demand an export for a function that never needed one, and it stayed
red until someone read the regex closely enough to disbelieve it.

A false alarm in a guard nobody can override is expensive: the honest responses
are to fix the parser or to export something that should not be exported, and
the second is easier.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_TOOL = Path(__file__).resolve().parents[2] / "tools" / "dev" / "check_handlers.py"
_spec = importlib.util.spec_from_file_location("check_handlers", _TOOL)
check_handlers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_handlers)

strip = check_handlers.without_interpolations


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # The case that was failing: the handler survives, the render-time call goes.
        ("openDocs('${esc(reader.docs)}')", "openDocs('')"),
        # Nothing to strip.
        ("showSection('library')", "showSection('library')"),
        # An interpolation holding braces of its own. A non-greedy `\\$\\{.*?\\}`
        # stops at the first `}` and leaves `)}` behind to be rescanned, which is
        # why this is brace-counted.
        ("go(${a ? `${b}` : c})", "go()"),
        ("go(${ {k: 1}.k })", "go()"),
        # Several in one attribute.
        ("f('${a(1)}', '${b(2)}')", "f('', '')"),
        # Unterminated: consume to the end rather than emit a stray tail.
        ("f('${oops", "f('"),
        # A `$` that is not an interpolation is ordinary text.
        ("cost('$5')", "cost('$5')"),
    ],
)
def test_interpolations_are_removed_and_nothing_else_is(body, expected):
    assert strip(body) == expected


def test_a_handler_named_by_an_interpolation_is_not_claimed():
    """Nothing static can say what it resolves to, so requiring an export for a
    guessed name would be the same false positive in a new place."""
    assert "fn" not in check_handlers._CALL.findall(strip("${fn}(1)"))


def test_the_real_frontend_passes_its_own_check():
    """The end the tool is actually for. Kept as an assertion rather than left
    to CI so that a change to either side fails next to the code that made it."""
    used = set().union(*check_handlers.handler_names().values())
    missing = used - check_handlers.exported_names()
    assert not missing, f"inline handlers main.js does not export: {sorted(missing)}"
