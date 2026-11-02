"""
MediaSweepService.sweep — the safety net under every job, every two minutes
(``media.sweep_jobs`` in CELERY_BEAT_SCHEDULE).

Three branches, each safe to run twice:

  1. LOST DISPATCH. A job is dispatched with fallback="skip", so a broker blip
     or a stale heartbeat at POST time drops it silently. A queued job nobody
     has touched for two minutes is dispatched again. Duplicates are harmless:
     MediaJobService.claim lets exactly one run through.
  2. DEAD RUN. A processing job older than the hard time limit plus a grace
     cannot still be running — Celery has killed it, or the OOM killer did. It
     goes back to the queue, or fails PROCESSING_TIMEOUT once it has used its
     attempts.
  3. FAILED SOURCE. A failed job keeps its original for a day (something to
     look at when a file breaks the converter), then the original is deleted.
  4. UNSETTLED PUBLICATION. A held post whose jobs are all ready, or one of
     them failed, but is still processing — its media_job_finished was missed
     (another run held the lock) — gets its try_publish.
  5. STALE PUBLICATION. One still waiting after two hours fails
     PROCESSING_TIMEOUT, and the author is told.

It never deletes an OUTPUT. A chat message or a recruitment references its
media by URL without knowing which job made it.

Not a branch: ``recover_after_restart`` runs once, when the media worker
starts (apps/media/worker_signals.py), and recovers a killed run at once
instead of after branch 2's ~32 minutes.
"""

import glob
import logging
import os
import shutil
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Count, F, Q
from django.utils import timezone

from apps.media.constants import (
    FAILED_SOURCE_TTL_SECONDS,
    MAX_ATTEMPTS,
    PROCESSING_TIMEOUT,
    PUBLICATION_TIMEOUT_SECONDS,
    QUEUED_REDISPATCH_AFTER_SECONDS,
    STUCK_GRACE_SECONDS,
    SWEEP_BATCH_SIZE,
)
from apps.media.models import MediaJob, PendingPublication
from apps.media.services.media_job_service import MediaJobService
from apps.media.services.media_processing_service import WORK_DIR_PREFIX
from apps.media.services.publication_service import PublicationService
from services.storage.factory import get_storage_service
from shared.tasks import delete_keys
from utils.background_jobs import FALLBACK_SKIP, enqueue

logger = logging.getLogger(__name__)


class MediaSweepService:

    @classmethod
    def sweep(cls, now=None) -> dict:
        now = now or timezone.now()

        redispatched = cls._redispatch_queued(now)
        requeued, timed_out = cls._recover_dead_runs(now)
        sources_deleted = cls._delete_failed_sources(now)
        # Settle before expiring: a publication whose media finished after a
        # long backlog should publish, not time out on the same sweep.
        publications_settled = cls._settle_publications()
        publications_expired = cls._expire_publications(now)

        return {
            "redispatched": redispatched,
            "requeued": requeued,
            "timed_out": timed_out,
            "sources_deleted": sources_deleted,
            "publications_settled": publications_settled,
            "publications_expired": publications_expired,
        }

    @classmethod
    def recover_after_restart(cls, now=None) -> dict:
        """
        The MEDIA worker's startup recovery (apps/media/worker_signals.py,
        which says why it is safe and when it runs).

        A deploy or an OOM kill stops a conversion dead and leaves its job
        `processing` — branch 2 would only call it dead after the hard limit
        plus the grace (~32 minutes), and charge it a second attempt. At
        startup no conversion can be running yet, so every `processing` job
        is a dead run: back to `queued` with attempts unchanged (the dead run
        already spent one) and dispatched again. The temp folders a
        hard-killed run leaves under MEDIA_WORK_DIR go too.
        """
        now = now or timezone.now()

        with transaction.atomic():
            job_ids = list(
                MediaJob.objects
                .select_for_update(skip_locked=True)
                .filter(status=MediaJob.Status.PROCESSING)
                .values_list("id", flat=True)
            )
            MediaJob.objects.filter(id__in=job_ids).update(
                status=MediaJob.Status.QUEUED, updated_at=now
            )

        cls._dispatch(job_ids)

        removed = 0
        pattern = os.path.join(settings.MEDIA_WORK_DIR, f"{WORK_DIR_PREFIX}*")
        for path in glob.glob(pattern):
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
                removed += 1

        if job_ids or removed:
            logger.warning(
                "media.worker_ready | interrupted runs requeued | jobs=%s | "
                "work_dirs_removed=%s",
                len(job_ids), removed,
            )
        return {"requeued": len(job_ids), "work_dirs_removed": removed}

    @staticmethod
    def _dispatch(job_ids):
        # Local import: tasks.py imports this module.
        from apps.media.tasks import process_media_job

        for job_id in job_ids:
            enqueue(process_media_job, (str(job_id),), fallback=FALLBACK_SKIP)

    @classmethod
    def _redispatch_queued(cls, now) -> int:
        """
        Branch 1. Keyed on updated_at, which starts equal to created_at and is
        bumped on every re-dispatch: a job waiting behind a long backlog is
        re-sent once per two minutes, not once per sweep.
        """
        cutoff = now - timedelta(seconds=QUEUED_REDISPATCH_AFTER_SECONDS)

        with transaction.atomic():
            job_ids = list(
                MediaJob.objects
                .select_for_update(skip_locked=True)
                .filter(status=MediaJob.Status.QUEUED, updated_at__lt=cutoff)
                .order_by("created_at")
                .values_list("id", flat=True)[:SWEEP_BATCH_SIZE]
            )
            MediaJob.objects.filter(id__in=job_ids).update(updated_at=now)

        cls._dispatch(job_ids)
        return len(job_ids)

    @classmethod
    def _recover_dead_runs(cls, now):
        """Branch 2. Returns (requeued, timed_out) counts."""
        cutoff = now - timedelta(
            seconds=settings.MEDIA_JOB_TIME_LIMIT + STUCK_GRACE_SECONDS
        )
        requeue_ids = []
        timed_out = []

        with transaction.atomic():
            jobs = list(
                MediaJob.objects
                .select_for_update(skip_locked=True)
                .filter(
                    status=MediaJob.Status.PROCESSING, started_at__lt=cutoff
                )
                .order_by("started_at")[:SWEEP_BATCH_SIZE]
            )
            for job in jobs:
                if job.attempts < MAX_ATTEMPTS:
                    requeue_ids.append(job.id)
                else:
                    MediaJobService.mark_failed(
                        job, PROCESSING_TIMEOUT,
                        f"no run finished in {job.attempts} attempts",
                    )
                    timed_out.append(job)

            MediaJob.objects.filter(id__in=requeue_ids).update(
                status=MediaJob.Status.QUEUED, updated_at=now
            )

        for job in timed_out:
            MediaJobService.finish(job)
        cls._dispatch(requeue_ids)

        if requeue_ids or timed_out:
            logger.warning(
                "media.sweep_jobs | dead runs | requeued=%s | timed_out=%s",
                len(requeue_ids), len(timed_out),
            )
        return len(requeue_ids), len(timed_out)

    @staticmethod
    def _delete_failed_sources(now) -> int:
        """
        Branch 3. Each job is asked about once: ``source_deleted_at`` is
        stamped whether or not the object was still there, so a year of old
        failures is not a year of HEAD requests every two minutes.
        """
        cutoff = now - timedelta(seconds=FAILED_SOURCE_TTL_SECONDS)
        jobs = list(
            MediaJob.objects
            .filter(
                status=MediaJob.Status.FAILED,
                finished_at__lt=cutoff,
                source_deleted_at__isnull=True,
            )
            .order_by("finished_at")[:SWEEP_BATCH_SIZE]
        )
        if not jobs:
            return 0

        storage = get_storage_service()
        existing_keys = []
        checked_ids = []

        for job in jobs:
            try:
                exists = storage.head_object(job.source_key) is not None
            except Exception:
                # R2 is unreachable: every other HEAD would fail the same way.
                # Nothing stamped past this point, so the next sweep retries.
                logger.warning(
                    "media.sweep_jobs | could not check a failed source | "
                    "key=%s", job.source_key, exc_info=True,
                )
                break
            if exists:
                existing_keys.append(job.source_key)
            checked_ids.append(job.id)

        if existing_keys:
            enqueue(delete_keys, (existing_keys,))
        MediaJob.objects.filter(id__in=checked_ids).update(
            source_deleted_at=now
        )
        return len(existing_keys)

    @staticmethod
    def _settle_publications() -> int:
        """
        Branch 4 — recovers a lost signal. try_publish locks each row with
        skip_locked and re-checks everything, so a publication the signal is
        settling right now is simply skipped.
        """
        candidates = list(
            PendingPublication.objects
            .filter(status=PendingPublication.Status.PROCESSING)
            .annotate(
                total=Count("jobs"),
                ready=Count(
                    "jobs", filter=Q(jobs__status=MediaJob.Status.READY)
                ),
                failed=Count(
                    "jobs", filter=Q(jobs__status=MediaJob.Status.FAILED)
                ),
            )
            .filter(Q(failed__gt=0) | Q(ready=F("total")))
            .order_by("created_at")[:SWEEP_BATCH_SIZE]
        )

        settled = sum(
            1 for publication in candidates
            if PublicationService.try_publish(publication) is not None
        )
        if settled:
            logger.warning(
                "media.sweep_jobs | publications settled late | count=%s",
                settled,
            )
        return settled

    @staticmethod
    def _expire_publications(now) -> int:
        """Branch 5. PublicationService.expire notifies the author."""
        cutoff = now - timedelta(seconds=PUBLICATION_TIMEOUT_SECONDS)
        publication_ids = list(
            PendingPublication.objects
            .filter(
                status=PendingPublication.Status.PROCESSING,
                created_at__lt=cutoff,
            )
            .order_by("created_at")
            .values_list("id", flat=True)[:SWEEP_BATCH_SIZE]
        )
        return sum(
            1 for publication_id in publication_ids
            if PublicationService.expire(publication_id)
        )
