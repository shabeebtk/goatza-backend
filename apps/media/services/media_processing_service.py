"""
MediaProcessingService.run — one job, start to finish, on the worker.

    claim → preflight → head + download the source → decide output keys
    (saved BEFORE any upload) → convert → upload → ready → delete the source
    → finish()

The converters (apps/media/processing) know files, not jobs; this is the part
that knows jobs, R2 and the failure rules:

  * an EXPECTED failure (ProcessingError) marks the job failed with its code
    and returns normally — no Celery retry storm over a file that will fail
    the same way every time;
  * …except PROCESSOR_UNAVAILABLE at preflight, which is this worker's fault,
    not the file's: back to `queued`, no attempt spent (failed after an hour);
  * a download that failed on the network (SourceFetchError) hands the job
    back to `queued` and re-raises, so the task can retry it;
  * SoftTimeLimitExceeded → PROCESSING_TIMEOUT, recorded, not re-raised —
    unless the job already reached `ready`;
  * anything else → INTERNAL, logged with the traceback.
"""

import logging
import os
import tempfile
import time
import traceback

from botocore.exceptions import BotoCoreError, ClientError
from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.utils import timezone

from apps.media.constants import (
    INTERNAL,
    KIND_VIDEO,
    MAX_DURATION_SECONDS,
    OUTPUT_CACHE_CONTROL,
    OUTPUT_MAX_VIDEO_BYTES,
    PROCESSING_TIMEOUT,
    PROCESSOR_UNAVAILABLE,
    PROCESSOR_UNAVAILABLE_GIVE_UP_SECONDS,
    SOURCE_MISSING,
    SOURCE_TOO_LARGE,
)
from apps.media.processing import image, video
from apps.media.processing.base import ProcessingError, SourceFetchError
from apps.media.services.media_job_service import MediaJobService, source_cap
from services.storage.factory import get_storage_service
from services.storage.paths import sibling_key
from services.storage.r2 import is_not_found, public_url_for
from shared.tasks import delete_keys
from utils.background_jobs import enqueue

logger = logging.getLogger(__name__)

# Left between our own deadline and Celery's soft limit, for the uploads and
# the bookkeeping after the last subprocess returns.
DEADLINE_MARGIN_SECONDS = 60

# Each job's temp folder under MEDIA_WORK_DIR. A hard-killed run leaves its
# folder behind; the media worker deletes these at startup
# (MediaSweepService.recover_after_restart).
WORK_DIR_PREFIX = "media-job-"


class MediaProcessingService:

    @classmethod
    def run(cls, job_id):
        """Process one job. Raises SourceFetchError only — see the module."""
        deadline = (
            time.monotonic()
            + settings.MEDIA_JOB_SOFT_TIME_LIMIT
            - DEADLINE_MARGIN_SECONDS
        )

        job = MediaJobService.claim(job_id)
        if job is None:
            return

        try:
            cls._preflight(job.kind)
        except ProcessingError as exc:
            cls._not_on_this_worker(job, exc)
            return

        try:
            cls._process(job, deadline)
        except SourceFetchError:
            MediaJobService.release(job)
            raise
        except ProcessingError as exc:
            MediaJobService.mark_failed(job, exc.code, exc.detail)
        except SoftTimeLimitExceeded:
            # The limit can land AFTER mark_ready (during the source's delete
            # dispatch), and a ready job must stay ready. Re-read under the
            # lock first, as fail_unfinished does.
            failed = MediaJobService.fail_unfinished(
                job.id, PROCESSING_TIMEOUT, "Celery soft time limit exceeded"
            )
            if failed is not None:
                job = failed
            else:
                job.refresh_from_db()
        except Exception:
            logger.error(
                "media.process_job | unexpected failure | job=%s", job.id,
                exc_info=True,
            )
            MediaJobService.mark_failed(job, INTERNAL, traceback.format_exc())

        MediaJobService.finish(job)

    @classmethod
    def give_up(cls, job_id, detail):
        """The download retries are spent: fail the job INTERNAL."""
        job = MediaJobService.fail_unfinished(job_id, INTERNAL, detail)
        if job is not None:
            MediaJobService.finish(job)

    @staticmethod
    def _not_on_this_worker(job, exc):
        """
        PROCESSOR_UNAVAILABLE — THIS box has no ffmpeg / pillow-heif. Not the
        file's fault: logged at ERROR (a deploy to fix) and handed back to
        `queued` WITHOUT spending an attempt, so the sweeper re-dispatches it
        to a worker that can (an old image during a cutover is minutes from
        gone). Once the job is an hour old it is a broken deploy, not a
        cutover, and the job fails rather than loop forever.
        """
        age = (timezone.now() - job.created_at).total_seconds()
        if age > PROCESSOR_UNAVAILABLE_GIVE_UP_SECONDS:
            MediaJobService.mark_failed(job, exc.code, exc.detail)
            MediaJobService.finish(job)
            return

        logger.error(
            "media.process_job | this worker cannot convert, requeued | "
            "job=%s | %s",
            job.id, exc.detail,
        )
        MediaJobService.release(job, refund_attempt=True)

    # -----------------------------------------------------------
    # the work
    # -----------------------------------------------------------
    @classmethod
    def _process(cls, job, deadline):
        storage = get_storage_service()

        # Sized again here, not only at POST /media/jobs: a presigned PUT
        # binds the content type but not the length, and the URL outlives the
        # job's creation by up to an hour.
        head = cls._fetch(lambda: storage.head_object(job.source_key))
        if head is None:
            raise ProcessingError(SOURCE_MISSING, "source object is gone")
        if (head["size_bytes"] or 0) > source_cap(job.kind):
            raise ProcessingError(
                SOURCE_TOO_LARGE, f"{head['size_bytes']} bytes"
            )

        with tempfile.TemporaryDirectory(
            dir=settings.MEDIA_WORK_DIR, prefix=WORK_DIR_PREFIX
        ) as work_dir:
            extension = job.source_key.rpartition(".")[2]
            source_path = os.path.join(work_dir, f"source.{extension}")
            cls._fetch(
                lambda: storage.download_to_file(job.source_key, source_path)
            )

            cls._assign_output_keys(job)

            if job.kind == KIND_VIDEO:
                result = video.process(
                    source_path, work_dir,
                    max_duration=MAX_DURATION_SECONDS[job.purpose],
                    max_bytes=OUTPUT_MAX_VIDEO_BYTES[job.purpose],
                    deadline=deadline,
                )
                output_type = "video/mp4"
            else:
                result = image.process(source_path, work_dir)
                output_type = "image/jpeg"

            storage.upload_file(
                result.output_path, job.output_key, output_type,
                OUTPUT_CACHE_CONTROL,
            )
            storage.upload_file(
                result.thumbnail_path, job.thumbnail_key, "image/jpeg",
                OUTPUT_CACHE_CONTROL,
            )

        MediaJobService.mark_ready(
            job, result,
            output_url=public_url_for(job.output_key),
            thumbnail_url=public_url_for(job.thumbnail_key),
        )

        # The original has done its job. delete_keys retries on its own.
        enqueue(delete_keys, ([job.source_key],))

    @staticmethod
    def _preflight(kind):
        """
        Is this box able to convert `kind` at all? A "no" is a deploy mistake
        (the web dyno's image, a missing apt package), never the user's — so
        PROCESSOR_UNAVAILABLE, which logs at ERROR.
        """
        if kind == KIND_VIDEO:
            if not video.tools_available():
                raise ProcessingError(
                    PROCESSOR_UNAVAILABLE,
                    f"not on PATH: {settings.FFMPEG_BIN} / "
                    f"{settings.FFPROBE_BIN}",
                )
        elif not image.imaging_available():
            raise ProcessingError(
                PROCESSOR_UNAVAILABLE, "Pillow / pillow-heif not importable"
            )

        os.makedirs(settings.MEDIA_WORK_DIR, exist_ok=True)

    @staticmethod
    def _fetch(call):
        """
        Run an R2 read, sorting its failures: a missing object is the user's
        (SOURCE_MISSING), a network or R2 error is transient and retried.
        """
        try:
            return call()
        except ClientError as e:
            if is_not_found(e):
                raise ProcessingError(SOURCE_MISSING, str(e))
            raise SourceFetchError(str(e)) from e
        except (BotoCoreError, OSError) as e:
            raise SourceFetchError(f"{type(e).__name__}: {e}") from e

    @staticmethod
    def _assign_output_keys(job):
        """
        Decide where the outputs go — ONCE. A rerun (redelivery, sweeper
        recovery) finds the keys already saved and writes the same objects
        again, instead of leaving the first run's files orphaned beside a
        second set nobody references.
        """
        if job.output_key and job.thumbnail_key:
            return

        extension = ".mp4" if job.kind == KIND_VIDEO else ".jpg"
        job.output_key = sibling_key(job.source_key, extension)
        job.thumbnail_key = sibling_key(job.source_key, ".jpg")
        job.save(update_fields=["output_key", "thumbnail_key", "updated_at"])
