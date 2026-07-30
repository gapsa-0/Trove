"""Shared test utilities. Importable from every tier via pytest's `pythonpath`.

Not fixtures: these are called from module-level helper functions too, and a
plain function cannot request a fixture.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from unittest import mock


@contextmanager
def serve_in_thread(cfg):
    """Run ``serve(cfg)`` on an ephemeral port in a daemon thread.

    Yields the bound ``ThreadingHTTPServer`` -- read ``.server_address`` for the
    host and port -- and tears the thread down on exit. This is the one lifecycle
    every test making more than one request against a live server needs.

    Two things ``serve()`` does are handled here unconditionally rather than left
    as a caller obligation that is easy to forget:

    * ``JobManager.__init__`` starts a scheduler thread immediately
      (``gui/jobs.py``) which is free to pick up a registered archive and start
      really scanning, hashing and detecting it. Pausing first means a test that
      never thought about the pipeline still cannot start real background work.
    * ``serve()`` starts a thread loading the ~283 MB SigLIP text tower through
      ``semantic.warm_text_model``, and a developer machine has those weights, so
      it would really load them. That function is a documented best-effort warmup
      with no observable behaviour of its own, so stubbing it changes nothing a
      test could assert on.

    Lives here, in a uniquely-named module, rather than in ``tests/gui/conftest``:
    both that file and the root ``tests/conftest.py`` are importable as the module
    name `conftest`, so `from conftest import ...` resolves by sys.path order and
    would break silently if that order ever changed. `pythonpath = ["tests"]`
    makes this import unambiguous.
    """
    # Imported inside the function so the unit tier can use wait_until without
    # pulling in the HTTP server and its dependencies.
    from organize_archive.gui import semantic
    from organize_archive.gui.server import serve

    cfg.pipeline_paused = True
    with mock.patch.object(semantic, "warm_text_model", lambda cfg: None):
        httpd = serve(cfg, port=0)
    # serve_forever's default poll_interval is 0.5s and shutdown() blocks until
    # the loop notices the flag on its next poll, so every test would otherwise
    # pay a flat ~0.5s teardown for nothing -- there is no work being polled for
    # here beyond "was shutdown() called". Across a route suite that is tens of
    # seconds of idle waiting.
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


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
