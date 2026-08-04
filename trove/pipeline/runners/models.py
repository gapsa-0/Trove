"""The fetch job: get this archive's model weights before anything needs them.

Not a stage. It has no backlog in the catalogue, no place in the dependency
order and no card of its own -- it is the one-time cost of the features the
archive was created with, paid at the moment it was created rather than hours
later.

That timing is the whole point. Weights used to arrive on first *use*, which
for Search by description means after scan, enrich and dedup have finished:
someone chooses the feature, watches an archive scan all afternoon, and only
when they finally come back to search does a 689 MB download begin. The
scheduler starts this job as soon as an archive with missing weights is open
(``scheduler._start_fetch``), so the download runs alongside the scan and is
long finished by the time the stage that needs it is reachable.

It is a shortcut, never a gate. Every stage still fetches its own weights if
they are absent -- this job failing, being cancelled, or never having run at
all leaves the old behaviour exactly as it was, which is why nothing waits on
it and its failure is not reported on any card. The one thing the scheduler
does honour is not starting a stage whose weights are being fetched *right
now* (``stages.MODEL_KINDS``), since that would only download them twice.
"""

from __future__ import annotations

import logging

from ... import features
from ...errors import ModelUnavailableError
from ...services import models
from ..job import JobContext, Runner

logger = logging.getLogger(__name__)

KIND = "models"


def _reporter(ctx: JobContext) -> models.Log:
    """Where a download's progress line goes, and where cancelling is noticed.

    The line is the only progress this job reports. The sizes it is made of are
    known per file and not per feature -- a zip that is extracted, three files
    from one repository, one manifest entry -- so a percentage across the whole
    job would be a number invented here; the sidebar shows the line itself
    instead, over the indeterminate bar it already draws for work with no total.

    It doubles as this job's cancellation checkpoint: ``urlretrieve`` runs one
    long native-free loop with nothing else to check an event from, and its
    report hook (throttled to about one call a second by
    ``model_manifest.download_progress``) is the one place inside it that is
    ours. Raising from here unwinds through the backend's own ``finally``, which
    removes the partial file.
    """

    def log(message: str) -> None:
        ctx.raise_if_cancelled()
        ctx.job.current = message

    return log


def run(ctx: JobContext) -> None:
    root_id = ctx.job.require_root()
    enabled = ctx.cfg.archive_features(root_id)
    wanted = models.missing(ctx.cfg, enabled)
    if not wanted:
        ctx.job.message = "every model this archive needs is already here"
        return
    log = _reporter(ctx)
    done: list[str] = []
    failed: list[str] = []
    for feature_id in wanted:
        ctx.raise_if_cancelled()
        feature = features.by_id(feature_id)
        label = feature.label if feature else feature_id
        log(f"downloading the models for {label}")
        try:
            models.fetch(ctx.cfg, feature_id, log=log)
            done.append(label)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            # One feature's weights failing must not cost the others theirs:
            # People being unobtainable says nothing about SigLIP. Recorded
            # here, re-raised together below so the job ends as an error and
            # the scheduler's cooldown applies to the retry.
            logger.warning("could not fetch the %s models: %s", feature_id, exc)
            failed.append(f"{label} ({exc})")
    ctx.job.current = ""
    if failed:
        raise ModelUnavailableError("could not download the models for " + ", ".join(failed))
    ctx.job.message = "downloaded the models for " + ", ".join(done)


RUNNER = Runner(kind=KIND, run=run, takes_write_lock=False, needs_connection=False)
