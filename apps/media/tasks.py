# media/tasks.py
"""
Celery tasks for server-side media processing.

THIN, per CLAUDE.md ("Background jobs"): each task takes an id (or nothing),
calls a service, and owns only what is Celery's — the time limits and the
retry. The work itself is MediaProcessingService / MediaSweepService.

process_media_job is the one task in the app with its OWN time limits
(MEDIA_JOB_SOFT_TIME_LIMIT / MEDIA_JOB_TIME_LIMIT, 1500s / 1620s): a 5-minute
1080p clip on a half-CPU worker takes 8–12 minutes to encode, and the global
540s / 600s would kill it every time. Both stay under the broker's 3600s
visibility timeout, so an acked-late job is never redelivered while it runs.
"""

import logging

from celery import shared_task
from django.conf import settings

from apps.media.constants import (
    SOURCE_FETCH_MAX_RETRIES,
    SOURCE_FETCH_RETRY_COUNTDOWN,
)
from apps.media.processing.base import SourceFetchError
from apps.media.services.media_processing_service import MediaProcessingService
from apps.media.services.media_sweep_service import MediaSweepService

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    name="media.process_job",
    acks_late=True,
    soft_time_limit=settings.MEDIA_JOB_SOFT_TIME_LIMIT,
    time_limit=settings.MEDIA_JOB_TIME_LIMIT,
)
def process_media_job(self, job_id):
    """
    Convert one uploaded source into its ready files.

    ``acks_late=True`` — RE-RUNNING IS SAFE. A ready or failed job returns at
    once (MediaJobService.claim), a run that finds another live run's claim
    returns too, and the output keys are written to the job BEFORE anything
    is uploaded — so a rerun overwrites the same objects instead of orphaning
    a new set. A worker that dies mid-encode therefore costs a redelivery,
    never a lost job; the attempts counter stops a file that kills the worker
    from killing it forever.

    ALWAYS DISPATCHED WITH ``fallback="skip"``. Inline would run ffmpeg inside
    a web request, on a dyno that has no ffmpeg. A dispatch that was skipped
    leaves the job queued, and ``media.sweep_jobs`` dispatches it again.

    Only a failed DOWNLOAD is retried here (network, R2 5xx), 30s apart, twice;
    then the job fails INTERNAL. Every other failure is recorded on the job by
    the service and returns normally.
    """
    try:
        MediaProcessingService.run(job_id)
    except SourceFetchError as exc:
        if self.request.retries < SOURCE_FETCH_MAX_RETRIES:
            logger.warning(
                "media.process_job | source download failed, retrying | "
                "job=%s | attempt=%s | %s",
                job_id, self.request.retries + 1, exc,
            )
            raise self.retry(
                exc=exc,
                countdown=SOURCE_FETCH_RETRY_COUNTDOWN,
                max_retries=SOURCE_FETCH_MAX_RETRIES,
            )
        MediaProcessingService.give_up(
            job_id,
            f"source download failed after {SOURCE_FETCH_MAX_RETRIES} "
            f"retries: {exc}",
        )


@shared_task(name="media.sweep_jobs", acks_late=True)
def sweep_media_jobs():
    """
    Re-dispatch lost jobs, recover dead runs, delete day-old failed sources,
    settle held publications whose signal was missed, expire ones that waited
    two hours. Every two minutes (CELERY_BEAT_SCHEDULE).

    ``acks_late=True`` — EVERY BRANCH IS IDEMPOTENT. A re-dispatched job is
    claimed by at most one run; a dead run is requeued or failed under a row
    lock (skip_locked), and a second sweep finds it no longer stuck; a source
    delete is a no-op on a key that is already gone, and the job is stamped so
    it is not asked about again; a publication is settled or expired only
    while it is still processing, under the same kind of lock.
    """
    result = MediaSweepService.sweep()
    logger.info("media.sweep_jobs | done | %s", result)
