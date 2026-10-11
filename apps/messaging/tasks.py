# messaging/tasks.py
"""
Celery tasks for messaging.

THIN, per CLAUDE.md ("Background jobs"): the task takes an id, reloads the
message and fans the chat push out to the other participants. The realtime
channel-layer send stays on the request thread in ``message_service`` — a
websocket frame that arrives a second late is a bubble that appears a second
late, which is exactly what this task must not do to the open conversation.
"""

import logging

from celery import shared_task

from apps.messaging.models import ConversationParticipant, Message
from apps.notifications.services.deeplink_service import build_conversation_url
from apps.notifications.services.fcm_service import FCMService
from apps.notifications.services.notification_service import (
    NotificationService,
    get_org_admin_users,
)

logger = logging.getLogger(__name__)


@shared_task(name="messaging.push_message", acks_late=False)
def push_message(message_id):
    """
    Push one chat message to every participant except its sender.

    Moved here wholesale from ``MessageService._trigger_push``, which ran it on
    the request thread: a participant query, then an HTTPS round trip to
    Firebase per recipient, inside the send. A group conversation made that
    cost grow with the number of members while the sender watched a spinner.

    ``acks_late=False``, inverting the project-wide ``task_acks_late`` default
    for the same reason as ``emails.send`` and ``notifications.push``: a push is
    not idempotent, there is no per-message "pushed" column, and a worker dying
    after the Firebase call but before the ack would re-notify the whole
    conversation on redelivery. AT MOST ONCE — the message itself is already
    committed and will be read in the app regardless.

    SHARED MESSAGES still go through ``NotificationService.message_share``,
    which writes the in-app row and sends its own push. That write now happens
    in the worker rather than in the request; the message row is committed
    before this task is published, so the notification it derives always has
    its message to point at.

    Takes the message id as a string — the serializer is JSON.
    """
    message = (
        Message.objects
        # The conversation for the participant query and the deep link; the
        # sender to exclude them from it, and their profile for the
        # "sender_name" the push carries.
        .select_related(
            "conversation",
            "sender_user",
            "sender_user__profile",
            "sender_org",
        )
        .filter(id=message_id)
        .first()
    )

    # Deleted before the worker got to it. Routine in chat — somebody unsending
    # a message they just sent is the common case, not a fault — so INFO.
    if message is None:
        logger.info(
            "messaging.push_message | message gone, nothing sent | id=%s",
            message_id,
        )
        return

    conversation = message.conversation

    participants = ConversationParticipant.objects.filter(
        conversation=conversation
    ).select_related("user", "org")

    if message.sender_user:
        participants = participants.exclude(user=message.sender_user)
    elif message.sender_org:
        participants = participants.exclude(org=message.sender_org)

    tried = 0
    sent = 0
    failed = 0

    for participant in participants:
        if message.message_type in Message.SHARED_TYPES:
            # Shares go through the notifications module: it writes the
            # in-app row (grouped per conversation, deduped per message)
            # and sends the push itself.
            tried += 1
            try:
                NotificationService.message_share(
                    message,
                    recipient_user=participant.user,
                    recipient_org=participant.org,
                )
                sent += 1
            except Exception:
                # Per participant, same reason as the token loop below: one
                # recipient's share row failing must not cost the rest of the
                # conversation its notification.
                failed += 1
                logger.warning(
                    "messaging.push_message | share notification failed | "
                    "message=%s | participant=%s",
                    message.id, participant.id, exc_info=True,
                )
            continue

        # Text/media keep the existing push-only behaviour — no in-app
        # notification row is written for ordinary chat.
        #
        # An org has no device of its own, so its push fans out to the
        # OWNER/ADMIN members the same way a notification row does. Without
        # this branch an org participant got no push at all for ordinary
        # chat — only for shares, which go through NotificationService above.
        if participant.user:
            targets = [participant.user]
        elif participant.org:
            targets = get_org_admin_users(participant.org)
        else:
            continue

        if not targets:
            continue

        # Caption if there is one, else a media-type-specific line.
        if message.content:
            body = message.content[:50]
        elif message.message_type == Message.Type.IMAGE:
            body = "📷 Sent you a photo"
        elif message.message_type == Message.Type.VIDEO:
            body = "🎥 Sent you a video"
        else:
            body = ""

        payload = {
            "type": "message",
            "title": "New message",
            "body": body,
            "conversation_id": str(conversation.id),
            "sender_name": message.sender_user.profile_name
            if message.sender_user else "",
            # Resolved in the RECIPIENT's route space — an org member opening
            # this must land inside /organization/admin/<id>/… or the client
            # switches them back to their personal account.
            "url": build_conversation_url(conversation.id, participant.org_id),
        }

        for target in targets:
            tried += 1
            try:
                FCMService.send_to_user(target, payload)
                sent += 1
            except Exception:
                # PER RECIPIENT: an org conversation fans out to every
                # OWNER/ADMIN, and one unreachable device must not stop the
                # others being told. FCMService deactivates dead tokens itself;
                # an exception out of it is the transport or Firebase init.
                failed += 1
                logger.warning(
                    "messaging.push_message | recipient failed | message=%s | "
                    "user=%s",
                    message.id, target.id, exc_info=True,
                )

    logger.info(
        "messaging.push_message | done | message=%s | conversation=%s | "
        "type=%s | tried=%s | sent=%s | failed=%s",
        message.id, conversation.id, message.message_type, tried, sent, failed,
    )

    # Nobody was reached though somebody should have been: a dead push channel
    # rather than one bad device, which is worth a Sentry event.
    if failed and not sent:
        logger.error(
            "messaging.push_message | every recipient failed | message=%s | "
            "conversation=%s | tried=%s",
            message.id, conversation.id, tried,
        )
