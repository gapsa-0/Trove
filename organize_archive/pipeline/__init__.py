"""The background pipeline: what work exists, what runs next, how it's done.

This package is the application-layer home for the archive's background
processing. It answers three questions:

* **What work exists** -- the fixed set of stages (scan, enrich, dedup,
  places, detect, semantic) and how their pending backlog is derived from the
  catalog, in ``stages.py``.
* **What runs next** -- dependency order between stages, and which one the
  scheduler should start given what is already running or paused.
* **How each kind of work is performed** -- the actual per-stage logic that
  does the scanning, enriching, deduplicating, and so on.

Later steps of this refactor add ``manager.py``, ``scheduler.py``,
``archives.py`` and a ``runners/`` package alongside ``stages.py``.
"""
