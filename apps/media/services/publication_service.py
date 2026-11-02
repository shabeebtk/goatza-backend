"""
PublicationService — a post or highlight held while its media converts, and
the ONE place it becomes real.

    POST /posts/create or /highlights/create, a job still converting
        → create()        a PendingPublication, 202 to the author
    media_job_finished (apps/media/receivers.py), or the sweeper
        → try_publish()   every job ready → the normal create path, as the
                          author; any job failed → failed + notification

The author can tap Post and leave. Nothing about the post exists until
try_publish creates it through PostService / HighlightService — the same code
a browser-encoded upload runs in its own request — so no read path has a
"processing" state to filter, and mentions, hashtags and counters fire exactly
as they always do.

Every status change is announced on the owner's notification socket
(``publication_updated``). A FAILURE also writes an in-app notification and a
push, because the author has probably left the app; a success is announced on
the socket only.
"""

import logging

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.highlights.services.highlight_services import HighlightService
from apps.media.constants import (
    ACTOR_NO_LONGER_VALID,
    ACTOR_NO_LONGER_VALID_MESSAGE,
    ERROR_CODES,
    INTERNAL,
    PROCESSING_TIMEOUT,
    PUBLISH_INTERNAL_MESSAGE,
    PUBLISH_REJECTED,
)
from apps.media.models import MediaJob, PendingPublication
from apps.media.selectors.media_job_selectors import owner_filter
from apps.media.serializers.publication_serializers import (
    PendingPublicationSerializer,
)
from apps.media.services.media_job_service import MediaJobService
from apps.notifications.services.notification_service import NotificationService
from apps.organization.models import OrganizationMember
from apps.posts.services.post_service import PostCreateError, PostService
from core.actor import Actor
from utils.errors import flatten_validation_error
from utils.realtime import safe_group_send
from utils.validations import is_valid_uuid

logger = logging.getLogger(__name__)

ERROR_MESSAGE_MAX_CHARS = 500


class PublicationError(Exception):
    """A refused cancel / dismiss, in ``upload_error``'s argument order."""

    def __init__(self, message, status_code=400, code=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class _StillPending(Exception):
    """A job slipped back out of `ready` between the check and the publish."""


class PublicationService:

    # -----------------------------------------------------------
    # create — the 202 path
    # -----------------------------------------------------------
    @staticmethod
    def create(*, actor, user, target, payload, jobs) -> PendingPublication:
        """
        Hold a validated create whose media is still converting.

        ``payload`` is the validated body with its media items as written;
        ``jobs`` every MediaJob it references, ready or not. ``user`` is the
        human submitting — for an org post, who the org actor is rebuilt from
        when it publishes.
        """
        with transaction.atomic():
            publication = PendingPublication.objects.create(
                owner_user=actor.user if actor.is_user else None,
                owner_org=actor.organization if actor.is_org else None,
                created_by=user,
                target=target,
                payload=payload,
            )
            publication.jobs.set(jobs)

        logger.info(
            "media.publication | held | publication=%s | target=%s | jobs=%s",
            publication.id, target, len(jobs),
        )
        PublicationService._announce(publication)

        # A job that finished between the request's check and this row
        # existing found nothing to publish on its signal. Look once now so
        # that file does not wait for the sweeper.
        if PublicationService.try_publish(publication) is not None:
            publication.refresh_from_db()

        return publication

    # -----------------------------------------------------------
    # try_publish — the only way a held request becomes a post
    # -----------------------------------------------------------
    @staticmethod
    def try_publish(publication):
        """
        Publish ``publication`` if its media allows. Returns the new status,
        or None when nothing changed (still waiting, locked by another run,
        or no longer processing).

        SKIP_LOCKED: the signal of one job, the signal of another and the
        sweeper can all arrive at once, and exactly one of them should act.
        One that loses the lock walks away; if it held the last missing job,
        the sweeper settles the publication within two minutes.

        The create runs in a savepoint, so a refusal (the rail filled to 10
        meanwhile, a deleted sport) or a bug rolls back only the post and
        still records the failure on this row.
        """
        with transaction.atomic():
            row = (
                PendingPublication.objects
                .select_for_update(skip_locked=True)
                .filter(
                    id=publication.id,
                    status=PendingPublication.Status.PROCESSING,
                )
                .first()
            )
            if row is None:
                return None

            jobs = list(row.jobs.all())
            failed_job = next(
                (job for job in jobs if job.status == MediaJob.Status.FAILED),
                None,
            )

            if failed_job is not None:
                PublicationService._set_failed(
                    row,
                    failed_job.error_code or INTERNAL,
                    MediaJobService.job_error_message(failed_job),
                )
            elif any(job.status != MediaJob.Status.READY for job in jobs):
                return None
            else:
                rebuilt = PublicationService._rebuild_actor(row)
                if rebuilt is None:
                    PublicationService._set_failed(
                        row, ACTOR_NO_LONGER_VALID,
                        ACTOR_NO_LONGER_VALID_MESSAGE,
                    )
                else:
                    try:
                        with transaction.atomic():
                            result_id = PublicationService._publish(
                                row, *rebuilt
                            )
                    except _StillPending:
                        return None
                    except (PostCreateError, ValidationError,
                            PermissionDenied, NotFound) as exc:
                        PublicationService._set_failed(
                            row, PUBLISH_REJECTED,
                            PublicationService._rejection_message(exc),
                        )
                    except Exception:
                        logger.error(
                            "media.publication | publish raised | "
                            "publication=%s", row.id, exc_info=True,
                        )
                        PublicationService._set_failed(
                            row, INTERNAL, PUBLISH_INTERNAL_MESSAGE
                        )
                    else:
                        row.status = PendingPublication.Status.PUBLISHED
                        row.result_id = result_id
                        row.published_at = timezone.now()
                        row.save(update_fields=[
                            "status", "result_id", "published_at",
                            "updated_at",
                        ])
                        logger.info(
                            "media.publication | published | "
                            "publication=%s | %s=%s",
                            row.id, row.target, result_id,
                        )

        PublicationService._after_change(row)
        return row.status

    @staticmethod
    def _publish(row, actor, user):
        """
        The normal create path, as the author. The payload's job items are
        resolved again — every job is ready now, so each is replaced by its
        outputs and checked like any other upload. ``publication_id`` tells
        the one-job-one-post check that these jobs are this row's own.
        """
        if row.target == PendingPublication.Target.POST:
            cleaned = PostService.validate_create_payload(
                actor, user, row.payload, publication_id=row.id
            )
            if cleaned["pending_jobs"]:
                raise _StillPending()
            return PostService.create_post(actor, user, cleaned).id

        return HighlightService.create_highlight(
            actor, payload=row.payload, publication_id=row.id
        ).id

    @staticmethod
    def _rebuild_actor(row):
        """
        ``(actor, user)`` to publish as, or None when that is no longer
        allowed. Minutes have passed since the request: the same rules
        core.actor.resolve_actor applies are applied again — for an org, the
        submitter must STILL be a member and the org not suspended.
        """
        if row.owner_user_id:
            user = row.owner_user
            if not user.is_active:
                return None
            return Actor(actor_type="user", user=user), user

        org = row.owner_org
        user = row.created_by
        if user is None or not user.is_active or org.is_suspended:
            return None

        membership = OrganizationMember.objects.filter(
            user=user, organization=org
        ).first()
        if membership is None:
            return None

        return (
            Actor(
                actor_type="organization",
                organization=org,
                organization_member=membership,
            ),
            user,
        )

    @staticmethod
    def _rejection_message(exc):
        if isinstance(exc, PostCreateError):
            return exc.message or exc.error or PUBLISH_INTERNAL_MESSAGE
        if isinstance(exc, ValidationError):
            return flatten_validation_error(exc.detail)["message"]
        return str(exc.detail)

    # -----------------------------------------------------------
    # the author's own actions
    # -----------------------------------------------------------
    @staticmethod
    def cancel(actor, publication_id):
        """
        Stop a held post from being published. Owner only (404 for anyone
        else), and only while processing — a cancel that loses the race to a
        publish waits for the lock and is told it is too late.
        """
        with transaction.atomic():
            row = PublicationService._owned_for_update(actor, publication_id)

            if row.status == PendingPublication.Status.CANCELLED:
                return row

            if row.status != PendingPublication.Status.PROCESSING:
                raise PublicationError(
                    "This can no longer be cancelled",
                    code="publication_not_processing",
                )

            row.status = PendingPublication.Status.CANCELLED
            row.save(update_fields=["status", "updated_at"])

        logger.info("media.publication | cancelled | publication=%s", row.id)
        PublicationService._announce(row)
        return row

    @staticmethod
    def dismiss(actor, publication_id):
        """Hide a FAILED one from GET /media/publications. Owner only."""
        with transaction.atomic():
            row = PublicationService._owned_for_update(actor, publication_id)

            if row.status != PendingPublication.Status.FAILED:
                raise PublicationError(
                    "Only a failed post can be dismissed",
                    code="publication_not_failed",
                )

            if row.dismissed_at is None:
                row.dismissed_at = timezone.now()
                row.save(update_fields=["dismissed_at", "updated_at"])

        return row

    @staticmethod
    def _owned_for_update(actor, publication_id):
        row = None
        if is_valid_uuid(publication_id):
            row = (
                PendingPublication.objects
                .select_for_update()
                .filter(id=publication_id, **owner_filter(actor))
                .first()
            )
        if row is None:
            raise PublicationError("Not found", status_code=404)
        return row

    # -----------------------------------------------------------
    # the sweeper's timeout
    # -----------------------------------------------------------
    @staticmethod
    def expire(publication_id):
        """
        Fail a publication that has waited too long. True if it moved.
        Skips one another run holds — that run is settling it.
        """
        with transaction.atomic():
            row = (
                PendingPublication.objects
                .select_for_update(skip_locked=True)
                .filter(
                    id=publication_id,
                    status=PendingPublication.Status.PROCESSING,
                )
                .first()
            )
            if row is None:
                return False
            PublicationService._set_failed(
                row, PROCESSING_TIMEOUT, ERROR_CODES[PROCESSING_TIMEOUT]
            )

        PublicationService._after_change(row)
        return True

    # -----------------------------------------------------------
    # state + telling the author
    # -----------------------------------------------------------
    @staticmethod
    def _set_failed(row, error_code, message):
        row.status = PendingPublication.Status.FAILED
        row.error_code = error_code
        row.error_message = (message or "")[:ERROR_MESSAGE_MAX_CHARS]
        row.save(update_fields=[
            "status", "error_code", "error_message", "updated_at",
        ])
        logger.warning(
            "media.publication | failed | publication=%s | code=%s | %s",
            row.id, error_code, row.error_message,
        )

    @staticmethod
    def _after_change(row):
        """After the commit: the socket always, the notification on failure."""
        PublicationService._announce(row)

        if row.status != PendingPublication.Status.FAILED:
            return

        try:
            NotificationService.publication_failed(
                recipient_user=row.owner_user if row.owner_user_id else None,
                recipient_org=row.owner_org if row.owner_org_id else None,
                publication=row,
            )
        except Exception:
            # The row already says failed and the socket already said so; a
            # broken notification write must not raise into the worker or
            # the sweeper that got us here.
            logger.error(
                "media.publication | failure notification raised | "
                "publication=%s", row.id, exc_info=True,
            )

    @staticmethod
    def _announce(row):
        """``publication_updated`` to the owner's sockets. Never raises."""
        safe_group_send(
            f"user_notifications_{row.owner_org_id or row.owner_user_id}",
            {
                "type": "notification_message",
                "notification_type": "publication_updated",
                "publication": dict(PendingPublicationSerializer(row).data),
            },
            tag="media.publication_updated",
        )
