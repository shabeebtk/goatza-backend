# notifications/tasks.py
"""
Celery tasks for notifications.

THIN, per CLAUDE.md ("Background jobs"): the task takes an id, reloads the row
with the columns the payload needs and calls the same two service helpers the
request path used to call inline. Nothing about what a notification SAYS lives
here — that is ``build_notification_payload``, which the websocket path and the
tests also use.
"""

import logging

from celery import shared_task

from apps.notifications.models import Notification
from apps.notifications.services.fcm_service import FCMService
from apps.notifications.services.notification_service import (
    _get_recipient_users,
    build_notification_payload,
)

logger = logging.getLogger(__name__)


@shared_task(name="notifications.push", acks_late=False)
def push_notification(notification_id):
    """
    Send the FCM push for one already-persisted notification row.

    The row is written by ``NotificationService`` inside the request; this only
    delivers it. The in-app notification therefore appears whether or not FCM,
    the broker or the worker is healthy, which is the point of splitting them.

    ``acks_late=False``, inverting the project-wide ``task_acks_late`` default
    for the same reason as ``emails.send``: a push is not idempotent and there
    is no "pushed_at" column to check, so a worker dying between
    ``send_each_for_multicast`` and the ack would buzz the same phone twice on
    redelivery. AT MOST ONCE is the right trade for a notification whose in-app
    row is already safely stored.

    Takes the notification id as a string — the serializer is JSON and a model
    instance would not survive the trip.
    """
    notification = (
        Notification.objects
        # Everything build_notification_payload and _get_recipient_users touch:
        # the actor's display name and avatar come off its profile, the body
        # quotes the post/comment, the deep link reads the recruitment, and the
        # recipient is either the user row or the org whose admins get it.
        .select_related(
            "recipient_user",
            "recipient_org",
            "actor_user",
            "actor_user__profile",
            "actor_org",
            "actor_org__profile",
            "post",
            "comment",
            "recruitment",
        )
        .filter(id=notification_id)
        .first()
    )

    # Gone before the worker got to it: the row was deleted, or the
    # transaction that created it rolled back after the publish. Normal, not an
    # error — INFO and return.
    if notification is None:
        logger.info(
            "notifications.push | notification gone, nothing sent | id=%s",
            notification_id,
        )
        return

    payload = build_notification_payload(notification)
    recipients = _get_recipient_users(notification)

    sent = 0
    failed = 0
    for user in recipients:
        try:
            FCMService.send_to_user(user, payload)
            sent += 1
        except Exception:
            # PER RECIPIENT, so one unreachable device cannot cost the other
            # recipients their push. An org notification fans out to every
            # OWNER/ADMIN, and a single stale token taking the whole task down
            # would silently stop the rest of the club being told anything.
            # FCMService already handles dead tokens itself; an exception out
            # of it is the transport or Firebase init, not a token.
            failed += 1
            logger.warning(
                "notifications.push | recipient failed | id=%s | type=%s | "
                "user=%s",
                notification.id, notification.type, user.id, exc_info=True,
            )

    logger.info(
        "notifications.push | done | id=%s | type=%s | tried=%s | sent=%s | "
        "failed=%s",
        notification.id, notification.type, len(recipients), sent, failed,
    )

    # Nobody at all was reached though somebody should have been: not a
    # per-device hiccup but a dead push channel, and the kind of thing that is
    # worth a Sentry event rather than a WARNING nobody reads.
    if failed and not sent:
        logger.error(
            "notifications.push | every recipient failed | id=%s | type=%s | "
            "tried=%s",
            notification.id, notification.type, len(recipients),
        )
