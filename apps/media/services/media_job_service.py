"""
MediaJobService — the job's lifecycle: create, claim, finish.

Every status write goes through here, from the API (create), the worker
(claim, ready, failed) and the sweeper alike, so the realtime event and the
``media_job_finished`` signal leave from one place: ``finish``.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apps.accounts.views.user_upload_signature_views import (
    resolve_upload_actor,
)
from apps.highlights.models import Highlight
from apps.media.constants import (
    ERROR_CODES,
    INTERNAL,
    KIND_IMAGE,
    MAX_ATTEMPTS,
    OPERATIONAL_ERROR_CODES,
    PROCESSING_TIMEOUT,
    PURPOSE_UPLOAD_TYPES,
    PURPOSES,
    SERVER_PROCESSING_UNAVAILABLE,
    SERVER_PROCESSING_UNAVAILABLE_MESSAGE,
    SOURCE_MISSING,
    SOURCE_TOO_LARGE,
    SOURCE_UNREADABLE,
    SOURCE_UPLOAD_TYPES,
    STUCK_GRACE_SECONDS,
    VIDEO_ONLY_PURPOSES,
    error_message,
    source_kind,
    source_kind_for_extension,
)
from apps.media.models import MediaJob, PendingPublication
from apps.media.selectors.capability_selectors import (
    server_processing_available,
)
from apps.media.selectors.media_job_selectors import (
    job_for_owner,
    job_for_source_key,
)
from apps.media.serializers.media_job_serializers import MediaJobSerializer
from apps.media.signals import media_job_finished
from apps.posts.models import PostMedia
from services.storage.factory import get_storage_service
from services.storage.paths import is_source_key, is_upload_key_for
from utils.background_jobs import FALLBACK_SKIP, enqueue
from utils.realtime import safe_group_send
from utils.validations import is_valid_uuid

logger = logging.getLogger(__name__)

# error_detail is for a human reading the admin; a full ffmpeg log is not.
ERROR_DETAIL_MAX_CHARS = 8000


class MediaJobError(Exception):
    """A refused POST /media/jobs, in ``upload_error``'s argument order."""

    def __init__(self, message, field="non_field_errors", status_code=400,
                 code=None):
        super().__init__(message)
        self.message = message
        self.field = field
        self.status_code = status_code
        self.code = code


def source_cap(kind) -> int:
    if kind == KIND_IMAGE:
        return settings.MEDIA_SOURCE_MAX_IMAGE_BYTES
    return settings.MEDIA_SOURCE_MAX_VIDEO_BYTES


class MediaJobService:

    # -----------------------------------------------------------
    # create (POST /media/jobs)
    # -----------------------------------------------------------
    @staticmethod
    def create_job(*, request, actor, key, purpose, upload_type, org_id=None):
        """
        Queue the conversion of an uploaded source. Returns ``(job, created)``;
        raises MediaJobError for a refusal.

        IDEMPOTENT ON THE SOURCE KEY. A client that lost the response (an app
        switch, a dropped connection) posts again and gets the same job back,
        not a second conversion of the same file.

        ``request`` is only for ``resolve_upload_actor`` — the signature
        view's own function, reused so the org rules cannot drift between
        signing a source and claiming it.
        """
        if not server_processing_available():
            raise MediaJobError(
                SERVER_PROCESSING_UNAVAILABLE_MESSAGE,
                code=SERVER_PROCESSING_UNAVAILABLE,
            )

        if not isinstance(purpose, str) or purpose not in PURPOSES:
            raise MediaJobError("Invalid purpose", "purpose")

        if not isinstance(upload_type, str) \
                or upload_type not in SOURCE_UPLOAD_TYPES:
            raise MediaJobError("Invalid upload type", "upload_type")

        # The outputs land in the source's folder, so the pair decides where a
        # post's media lives — and what deleting it sweeps.
        if upload_type not in PURPOSE_UPLOAD_TYPES[purpose]:
            raise MediaJobError(
                "Invalid upload type for this purpose", "upload_type"
            )

        actor, refusal = resolve_upload_actor(
            request, actor, upload_type, org_id
        )
        if refusal:
            raise MediaJobError(*refusal)

        # A source key the signature endpoint could have issued to THIS actor
        # for THIS type — the same check multipart create makes.
        if not is_source_key(key) or not is_upload_key_for(
            actor, upload_type, key
        ):
            raise MediaJobError("Invalid key", "key")

        existing = job_for_source_key(actor, key)
        if existing:
            return existing, False

        head = get_storage_service().head_object(key)
        if head is None:
            raise MediaJobError(
                ERROR_CODES[SOURCE_MISSING], "key", code=SOURCE_MISSING
            )

        # The Content-Type was signed into the PUT, so R2's copy is ours, not
        # the client's. The extension is a fallback for an object stored
        # without one.
        content_type = (head["content_type"] or "").split(";")[0].strip().lower()
        kind = source_kind(content_type) or source_kind_for_extension(
            key.rpartition(".")[2]
        )
        if kind is None:
            raise MediaJobError(
                ERROR_CODES[SOURCE_UNREADABLE], "key", code=SOURCE_UNREADABLE
            )

        if kind == KIND_IMAGE and purpose in VIDEO_ONLY_PURPOSES:
            raise MediaJobError(
                "Images are not allowed for this upload type", "purpose"
            )

        size_bytes = head["size_bytes"] or 0
        if size_bytes > source_cap(kind):
            raise MediaJobError(
                ERROR_CODES[SOURCE_TOO_LARGE], "key", code=SOURCE_TOO_LARGE
            )

        try:
            with transaction.atomic():
                job = MediaJob.objects.create(
                    owner_user=actor.user if actor.is_user else None,
                    owner_org=actor.organization if actor.is_org else None,
                    created_by=request.user,
                    purpose=purpose,
                    upload_type=upload_type,
                    kind=kind,
                    source_key=key,
                    source_content_type=content_type,
                    source_size_bytes=size_bytes,
                )
        except IntegrityError:
            # Two posts for one key racing past the lookup above: the unique
            # source_key let one in, and that one is the answer for both.
            existing = job_for_source_key(actor, key)
            if existing:
                return existing, False
            raise

        # Local import: tasks.py imports this module.
        from apps.media.tasks import process_media_job

        # SKIP, never inline: inline would run ffmpeg inside this request, on
        # a web dyno that has no ffmpeg. A dropped dispatch leaves the job
        # queued, and the sweeper dispatches it again within minutes.
        enqueue(process_media_job, (str(job.id),), fallback=FALLBACK_SKIP)

        logger.info(
            "media.create_job | queued | job=%s | kind=%s | purpose=%s | "
            "size=%s",
            job.id, kind, purpose, size_bytes,
        )
        return job, True

    # -----------------------------------------------------------
    # attaching a job to a post / highlight
    # -----------------------------------------------------------
    @staticmethod
    def resolve_for_attach(actor, job_id, *, purpose):
        """
        The actor's own job for `purpose`, or None.

        None covers a malformed id, somebody else's job and a job made for a
        different purpose alike — the caller answers one generic "Invalid
        media", so another actor's job id is never confirmed to exist. The
        status is the caller's to read: ready attaches, failed is refused with
        ``job_error_message``, queued/processing waits.
        """
        if not is_valid_uuid(job_id):
            return None

        job = job_for_owner(actor, job_id)
        if job is None or job.purpose != purpose:
            return None
        return job

    @staticmethod
    def in_use(job, *, publication_id=None) -> bool:
        """
        Is this job already a post or highlight — held (a processing
        publication), published, or its outputs attached directly? One job,
        one post: a client that lost its 202 and posts again must not publish
        the same media twice, and deleting one copy would sweep the folder the
        other's media lives in.

        ``publication_id`` is the publication being published right now —
        its own jobs are not a second use.
        """
        held = job.publications.filter(status__in=(
            PendingPublication.Status.PROCESSING,
            PendingPublication.Status.PUBLISHED,
        ))
        if publication_id is not None:
            held = held.exclude(id=publication_id)
        if held.exists():
            return True

        if not job.output_key:
            return False

        return (
            PostMedia.objects.filter(public_id=job.output_key).exists()
            or Highlight.objects.filter(public_id=job.output_key).exists()
        )

    @staticmethod
    def job_error_message(job):
        """The sentence a failed job is refused with."""
        return error_message(job.error_code, job.purpose) \
            or ERROR_CODES[INTERNAL]

    @staticmethod
    def attach_fields(job) -> dict:
        """
        A READY job as the media fields an attach request carries — the same
        names a browser-made upload sends, so it then goes through the same
        validate_media / validate_thumbnail checks as any other item.
        """
        return {
            "file_url": job.output_url,
            "public_id": job.output_key,
            "thumbnail_url": job.thumbnail_url,
            "media_type": job.kind,
            "width": job.width,
            "height": job.height,
            "duration": job.duration,
            "size_bytes": job.size_bytes,
        }

    # -----------------------------------------------------------
    # the worker's claim
    # -----------------------------------------------------------
    @staticmethod
    def claim(job_id):
        """
        Mark a job ``processing`` for this run and return it — or None when
        there is nothing for this run to do.

        Several dispatches of one job are normal (the sweeper re-dispatches a
        queued job that waits in a backlog; Redis redelivers), so this is the
        one gate that keeps them from converting the same file twice:

          * gone, ready or failed           -> None
          * processing, started recently    -> None (another run owns it)
          * out of attempts                 -> failed PROCESSING_TIMEOUT
          * otherwise                       -> processing, attempts += 1

        "Recently" is the hard time limit plus a grace: a run older than that
        has been killed by Celery, so its claim is dead.
        """
        exhausted = None
        now = timezone.now()
        live_after = now - timedelta(
            seconds=settings.MEDIA_JOB_TIME_LIMIT + STUCK_GRACE_SECONDS
        )

        with transaction.atomic():
            job = MediaJob.objects.select_for_update().filter(id=job_id).first()

            if job is None:
                logger.info("media.process_job | job gone | job=%s", job_id)
                return None

            if job.status in (MediaJob.Status.READY, MediaJob.Status.FAILED):
                logger.info(
                    "media.process_job | already %s | job=%s",
                    job.status, job_id,
                )
                return None

            if job.status == MediaJob.Status.PROCESSING \
                    and job.started_at and job.started_at > live_after:
                logger.info(
                    "media.process_job | owned by a live run | job=%s", job_id
                )
                return None

            if job.attempts >= MAX_ATTEMPTS:
                # Every earlier run died without recording why — killed by
                # the hard limit or the OOM killer, which is what a file that
                # is too much for this worker looks like.
                MediaJobService.mark_failed(
                    job, PROCESSING_TIMEOUT,
                    f"gave up after {job.attempts} attempts that never "
                    f"finished",
                )
                exhausted = job
            else:
                job.status = MediaJob.Status.PROCESSING
                job.started_at = now
                job.attempts += 1
                job.save(update_fields=[
                    "status", "started_at", "attempts", "updated_at",
                ])

        if exhausted is not None:
            MediaJobService.finish(exhausted)
            return None

        return job

    @staticmethod
    def release(job, *, refund_attempt=False):
        """
        Hand a claimed job back to ``queued`` — before a Celery retry, so the
        retried run can claim it instead of finding it owned.

        ``refund_attempt`` undoes claim's ``attempts += 1``, for a run that
        never touched the file (a worker that cannot convert at all): the
        attempts counter is for files that kill the worker, not for workers.
        """
        fields = {"status": MediaJob.Status.QUEUED, "updated_at": timezone.now()}
        if refund_attempt:
            fields["attempts"] = F("attempts") - 1
        MediaJob.objects.filter(
            id=job.id, status=MediaJob.Status.PROCESSING
        ).update(**fields)

    # -----------------------------------------------------------
    # terminal states
    # -----------------------------------------------------------
    @staticmethod
    def mark_failed(job, error_code, detail=""):
        """
        Record a failure. The caller sends ``finish``.

        ERROR for a failure on our side (OPERATIONAL_ERROR_CODES: a deploy
        mistake, an undersized worker, a bug) so Sentry raises it; WARNING for
        a file that is simply unusable — that is the user's answer, not an
        incident.
        """
        job.status = MediaJob.Status.FAILED
        job.error_code = error_code
        job.error_detail = (detail or "")[-ERROR_DETAIL_MAX_CHARS:]
        job.finished_at = timezone.now()
        job.save(update_fields=[
            "status", "error_code", "error_detail", "finished_at",
            "updated_at",
        ])

        log = logger.error if error_code in OPERATIONAL_ERROR_CODES \
            else logger.warning
        log(
            "media.process_job | failed | job=%s | code=%s | %s",
            job.id, error_code, (detail or "")[-500:],
        )

    @staticmethod
    def fail_unfinished(job_id, error_code, detail=""):
        """
        Fail a job by id unless it already finished. Returns the failed job
        (the caller sends ``finish``) or None.
        """
        with transaction.atomic():
            job = MediaJob.objects.select_for_update().filter(id=job_id).first()
            if job is None or job.status in (
                MediaJob.Status.READY, MediaJob.Status.FAILED
            ):
                return None
            MediaJobService.mark_failed(job, error_code, detail)
        return job

    @staticmethod
    def mark_ready(job, result, *, output_url, thumbnail_url):
        """
        Record the outputs. ``source_deleted_at`` is stamped here because the
        caller dispatches the source's delete right after, and that task
        retries on its own.
        """
        now = timezone.now()
        job.status = MediaJob.Status.READY
        job.output_url = output_url
        job.thumbnail_url = thumbnail_url
        job.width = result.width
        job.height = result.height
        job.duration = result.duration
        job.size_bytes = result.size_bytes
        job.error_code = ""
        job.error_detail = ""
        job.finished_at = now
        job.source_deleted_at = now
        job.save(update_fields=[
            "status", "output_url", "thumbnail_url", "width", "height",
            "duration", "size_bytes", "error_code", "error_detail",
            "finished_at", "source_deleted_at", "updated_at",
        ])

        logger.info(
            "media.process_job | ready | job=%s | %sx%s | %ss | %s bytes",
            job.id, job.width, job.height, job.duration, job.size_bytes,
        )

    # -----------------------------------------------------------
    # the one exit
    # -----------------------------------------------------------
    @staticmethod
    def finish(job):
        """
        Tell the owner and the rest of the backend that ``job`` is done.

        The websocket event goes to the owner's notification group (the group
        UserNotificationsConsumer joins — the org's id for an org actor). It
        is a courtesy: GET /media/jobs/<id> answers the same question, and
        safe_group_send never raises.

        The signal is sent robustly (see apps/media/signals.py): a receiver
        that raises is logged and the job stays as it is.
        """
        payload = dict(MediaJobSerializer(job).data)
        recipient_id = job.owner_org_id or job.owner_user_id

        safe_group_send(
            f"user_notifications_{recipient_id}",
            {
                "type": "notification_message",
                "notification_type": "media_job_updated",
                "job": payload,
            },
            tag="media.job_updated",
        )

        for receiver, response in media_job_finished.send_robust(
            sender=MediaJob, job=job
        ):
            if isinstance(response, Exception):
                logger.error(
                    "media.job_finished | receiver raised | receiver=%r | "
                    "job=%s",
                    receiver, job.id, exc_info=response,
                )
