"""Shared test utilities. Importable from every tier via pytest's `pythonpath`.

Not fixtures: these are called from module-level helper functions too, and a
plain function cannot request a fixture.
"""

from __future__ import annotations

import time
from collections.abc import Callable


def wait_until[T](
    predicate: Callable[[], T],
    timeout: float = 5.0,
    interval: float = 0.01,
    what: str = "condition",
) -> T:
    """Poll until ``predicate()`` returns something truthy, then return it.

    Use this instead of sleeping for a guessed duration. A fixed sleep is wrong
    in both directions -- too short and the test is flaky on a loaded machine,
    too long and every run pays for the worst case -- and when it does fail it
    reports whatever assertion came next rather than "this never happened".

    ``what`` names the thing being waited for; it is all the failure message has
    to go on, so make it a phrase that reads after "timed out waiting for".
    """
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        # The one justified sleep in the suite: a bounded poll interval, not a
        # guess at how long the work takes. Short enough to stay invisible.
        time.sleep(interval)
