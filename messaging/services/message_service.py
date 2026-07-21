'''
1. Send message (text / media / shared content)
2. Validate sender
3. Handle request logic
4. Save message
5. Update conversation
6. Trigger async events (WebSocket + FCM)

Every send — text, media, or shared content — goes through
_create_and_dispatch, so persistence, conversation bookkeeping, realtime
fan-out and push happen in exactly one place.
'''


from django.utils import timezone
from django.db import transaction
from asgiref.sync import async_to_sync

from messaging.models import Message, Conversation, ConversationParticipant
from messaging.selectors.share_selectors import (
    ShareViewer,
    is_post_shareable,
    is_recruitment_shareable,
)
from messaging.services.exceptions import (
    ContentUnavailableError,
    EmptyMessageError,
    InvalidMediaError,
    InvalidSenderError,
    MessageNotFoundError,
    NotMessageSenderError,
    NotParticipantError,
)
from notifications.services.fcm_service import FCMService
from notifications.services.notification_service import NotificationService
from services.storage.validators import (
    build_video_thumbnail_url,
    extract_public_id_from_url,
    get_file_extension,
    is_valid_cloudinary_url,
)

# Chat images: matches the signature preset. HEIC is allowed on upload;
# Cloudinary serves a jpg/webp derivative to browsers via transformation.
CHAT_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "heic"}
CHAT_IMAGE_MAX_BYTES = 10 * 1024 * 1024  # 10 MB

# Chat videos.
CHAT_VIDEO_EXTENSIONS = {"mp4", "mov", "webm"}
CHAT_VIDEO_MAX_BYTES = 100 * 1024 * 1024  # 100 MB
CHAT_VIDEO_MAX_DURATION_MS = 90 * 1000    # 90 seconds


class MessageService:

    # MAIN ENTRY
    @staticmethod
    def send_message(
        conversation: Conversation,
        sender_user=None,
        sender_org=None,
        content: str = "",
        message_type: str = "text",
    ):
        """
        Main method to send message
        Works for:
        - API
        - WebSocket
        """

        # A text message with no body is meaningless. Enforced here rather than
        # by a DB CheckConstraint so the caller gets a typed error instead of an
        # IntegrityError — and so media/shared types, whose content is an
        # optional caption, stay unconstrained.
        if message_type == Message.Type.TEXT and not (content or "").strip():
            raise EmptyMessageError("Message content is required")

        return MessageService._create_and_dispatch(
            conversation,
            sender_user,
            sender_org,
            message_type=message_type,
            content=content,
        )

    # SHARE A POST
    @staticmethod
    def send_shared_post(
        conversation: Conversation,
        sender_user=None,
        sender_org=None,
        post=None,
        note: str = "",
    ):
        """
        Forward a post into a conversation. ``note`` is an optional caption and
        is stored as the message content.
        """
        viewer = ShareViewer(user=sender_user, org=sender_org)

        if not is_post_shareable(post, viewer):
            raise ContentUnavailableError("This post is no longer available")

        return MessageService._create_and_dispatch(
            conversation,
            sender_user,
            sender_org,
            message_type=Message.Type.SHARED_POST,
            content=note or "",
            shared_post=post,
        )

    # SHARE A RECRUITMENT
    @staticmethod
    def send_shared_recruitment(
        conversation: Conversation,
        sender_user=None,
        sender_org=None,
        recruitment=None,
        note: str = "",
    ):
        """
        Forward a recruitment into a conversation. ``note`` is an optional
        caption and is stored as the message content.
        """
        viewer = ShareViewer(user=sender_user, org=sender_org)

        if not is_recruitment_shareable(recruitment, viewer):
            raise ContentUnavailableError(
                "This recruitment is no longer available"
            )

        return MessageService._create_and_dispatch(
            conversation,
            sender_user,
            sender_org,
            message_type=Message.Type.SHARED_RECRUITMENT,
            content=note or "",
            shared_recruitment=recruitment,
        )

    # SEND AN IMAGE
    @staticmethod
    def send_image_message(
        conversation: Conversation,
        sender_user=None,
        sender_org=None,
        media_url: str = "",
        media_public_id: str = "",
        width=None,
        height=None,
        size_bytes=None,
        caption: str = "",
    ):
        """
        Send a photo message. The media has already been uploaded straight to
        Cloudinary by the client (signed upload) — we get the URL back, so we
        must re-validate it belongs to us before trusting it (see
        _validate_chat_image). width/height/size are client-reported and only
        drive layout, so they are stored as-is.
        """
        MessageService._validate_chat_image(
            sender_user, sender_org, media_url, media_public_id, size_bytes
        )

        return MessageService._create_and_dispatch(
            conversation,
            sender_user,
            sender_org,
            message_type=Message.Type.IMAGE,
            content=caption or "",
            media_url=media_url,
            media_public_id=media_public_id,
            media_width=width,
            media_height=height,
            media_size_bytes=size_bytes,
        )

    # SEND A VIDEO
    @staticmethod
    def send_video_message(
        conversation: Conversation,
        sender_user=None,
        sender_org=None,
        media_url: str = "",
        media_public_id: str = "",
        width=None,
        height=None,
        duration_ms=None,
        size_bytes=None,
        caption: str = "",
    ):
        """
        Send a video message. Like send_image_message, but validates the video
        constraints (format/size/duration) and derives the poster thumbnail
        server-side from the public_id (never trusts a client thumbnail).
        """
        MessageService._validate_chat_video(
            sender_user, sender_org, media_url, media_public_id,
            size_bytes, duration_ms,
        )

        return MessageService._create_and_dispatch(
            conversation,
            sender_user,
            sender_org,
            message_type=Message.Type.VIDEO,
            content=caption or "",
            media_url=media_url,
            media_public_id=media_public_id,
            media_thumbnail_url=build_video_thumbnail_url(media_public_id),
            media_width=width,
            media_height=height,
            media_duration_ms=duration_ms,
            media_size_bytes=size_bytes,
        )

    @staticmethod
    def _chat_media_prefix(sender_user, sender_org):
        """The sender's own chat subfolder — mirrors get_upload_config('chat')."""
        if sender_user:
            return f"chat/users/{sender_user.id}/"
        if sender_org:
            return f"chat/organizations/{sender_org.id}/"
        raise InvalidSenderError("Invalid sender")

    @staticmethod
    def _validate_chat_media_url(
        sender_user, sender_org, media_url, media_public_id, allowed_extensions
    ):
        """
        Shared "never trust the client URL" checks for image + video. Accepted
        only when it points at OUR cloud, has an allowed extension, lives under
        the SENDER's own chat folder (so another actor's URL can't be replayed),
        and the public_id embedded in the URL matches the one sent.
        """
        if not media_url or not is_valid_cloudinary_url(media_url):
            raise InvalidMediaError("Invalid media source")

        if get_file_extension(media_url) not in allowed_extensions:
            raise InvalidMediaError("Unsupported media format")

        prefix = MessageService._chat_media_prefix(sender_user, sender_org)
        if not media_public_id or not media_public_id.startswith(prefix):
            raise InvalidMediaError("Invalid media path")

        if extract_public_id_from_url(media_url) != media_public_id:
            raise InvalidMediaError("Media URL and public_id mismatch")

    @staticmethod
    def _validate_chat_image(
        sender_user, sender_org, media_url, media_public_id, size_bytes
    ):
        MessageService._validate_chat_media_url(
            sender_user, sender_org, media_url, media_public_id,
            CHAT_IMAGE_EXTENSIONS,
        )
        if size_bytes is not None and size_bytes > CHAT_IMAGE_MAX_BYTES:
            raise InvalidMediaError("Image exceeds the 10MB limit")

    @staticmethod
    def _validate_chat_video(
        sender_user, sender_org, media_url, media_public_id,
        size_bytes, duration_ms,
    ):
        MessageService._validate_chat_media_url(
            sender_user, sender_org, media_url, media_public_id,
            CHAT_VIDEO_EXTENSIONS,
        )
        if size_bytes is not None and size_bytes > CHAT_VIDEO_MAX_BYTES:
            raise InvalidMediaError("Video exceeds the 100MB limit")

        # Duration is client-reported (same trust model as image dimensions);
        # the 100MB size cap is the hard abuse bound, this gate is mostly UX.
        if duration_ms is not None and duration_ms > CHAT_VIDEO_MAX_DURATION_MS:
            raise InvalidMediaError("Video exceeds the 90 second limit")

    # ----------------------------------------
    # THE ONE WRITE PATH
    # ----------------------------------------
    @staticmethod
    def _create_and_dispatch(
        conversation,
        sender_user,
        sender_org,
        message_type,
        content="",
        **message_fields,
    ):
        """
        Validate → persist → update conversation → fan out.

        ``message_fields`` carries type-specific columns (shared_post,
        shared_recruitment, media_* …) straight through to the row.
        """
        MessageService._validate_sender(conversation, sender_user, sender_org)

        with transaction.atomic():
            message = Message.objects.create(
                conversation=conversation,
                sender_user=sender_user,
                sender_org=sender_org,
                content=content,
                message_type=message_type,
                **message_fields,
            )

            MessageService._update_conversation(conversation, message)

        # Unread state is derived (participant.last_read_at vs message
        # created_at), so there is no per-participant counter to bump — sending
        # a message makes it unread for everyone who hasn't read since. Same as
        # it has always worked for text.

        # REALTIME + PUSH (outside the transaction — a dead Redis or FCM must
        # never roll back a persisted message)
        MessageService._trigger_realtime(conversation, message)
        MessageService._trigger_push(conversation, message)

        return message

    # ----------------------------------------
    # DELETE (unsend)
    # ----------------------------------------
    @staticmethod
    def delete_message(conversation, message_id, actor_user=None, actor_org=None):
        """
        Unsend a message for everyone.

        Soft delete: the row is kept (``is_deleted=True``) — every read path
        already filters it out (message_selectors, the conversation serializers'
        last-message preview), so nothing else needs to change. Only the actor
        who SENT it may unsend it; being a participant is not enough.

        If the conversation's last_message pointed at it, the pointer is moved
        back to the newest surviving message so the chat list doesn't keep
        previewing something that no longer exists.
        """
        message = Message.objects.filter(
            id=message_id,
            conversation=conversation,
            is_deleted=False,
        ).first()

        if not message:
            raise MessageNotFoundError("Message not found")

        # Ownership: compare against the ACTING identity, so acting as an org
        # can't delete the same person's personal messages (and vice versa).
        if actor_org is not None:
            is_sender = message.sender_org_id == actor_org.id
        elif actor_user is not None:
            is_sender = (
                message.sender_org_id is None
                and message.sender_user_id == actor_user.id
            )
        else:
            raise InvalidSenderError("Invalid sender")

        if not is_sender:
            raise NotMessageSenderError("You can only delete your own messages")

        with transaction.atomic():
            message.is_deleted = True
            message.save(update_fields=["is_deleted"])

            if conversation.last_message_id == message.id:
                previous = (
                    Message.objects
                    .filter(conversation=conversation, is_deleted=False)
                    .order_by("-created_at")
                    .first()
                )
                conversation.last_message = previous
                conversation.last_message_at = (
                    previous.created_at if previous else None
                )
                conversation.save(
                    update_fields=["last_message", "last_message_at"]
                )

        # Outside the transaction, like the send path: a dead Redis must never
        # roll back a delete the user has already been shown as done.
        MessageService._trigger_realtime_delete(conversation, message)

        return message

    @staticmethod
    def _trigger_realtime_delete(conversation, message):
        from channels.layers import get_channel_layer

        channel_layer = get_channel_layer()

        async_to_sync(channel_layer.group_send)(
            f"chat_{conversation.id}",
            {
                "type": "message_deleted",
                "message_id": str(message.id),
            }
        )

        # Same conversation-list nudge the send path uses.
        participants = ConversationParticipant.objects.filter(
            conversation=conversation
        )
        for participant in participants:
            recipient_id = participant.user_id or participant.org_id
            if recipient_id:
                async_to_sync(channel_layer.group_send)(
                    f"user_notifications_{recipient_id}",
                    {
                        "type": "notification_message",
                        "notification_type": "conversation_updated",
                        "conversation_id": str(conversation.id),
                    }
                )

    # VALIDATION
    @staticmethod
    def _validate_sender(conversation, sender_user, sender_org):
        query = ConversationParticipant.objects.filter(
            conversation=conversation
        )

        if sender_user:
            query = query.filter(user=sender_user)
        elif sender_org:
            query = query.filter(org=sender_org)
        else:
            raise InvalidSenderError("Invalid sender")

        if not query.exists():
            raise NotParticipantError("Sender not part of conversation")

    # UPDATE CONVERSATION
    @staticmethod
    def _update_conversation(conversation, message):
        conversation.last_message = message
        conversation.last_message_at = timezone.now()
        conversation.save(update_fields=["last_message", "last_message_at"])

    # ----------------------------------------
    # REALTIME (WebSocket)
    # ----------------------------------------
    @staticmethod
    def _trigger_realtime(conversation, message):
        from channels.layers import get_channel_layer
        from messaging.serializers.message_serializers import MessageSerializer

        channel_layer = get_channel_layer()

        # One group_send serves every socket in the room, so the payload is
        # rendered with no viewer: previews that depend on who is looking
        # (followers-only content) come back "unavailable" here rather than
        # leaking to a participant who cannot see them. ChatConsumer re-renders
        # those few for its own actor — see chat_message().
        payload = MessageSerializer(message, context={"viewer": None}).data

        # ----------------------------------------
        # 🔥 SEND CHAT MESSAGE
        # ----------------------------------------
        async_to_sync(channel_layer.group_send)(
            f"chat_{conversation.id}",
            {
                "type": "chat_message",
                "message": payload,

                # DEPRECATED flat fields. The web client
                # (features/messages/hooks/useChatSocket.ts) still reads
                # message_id/content/sender/created_at off the top level and
                # drops anything without them. Keep sending both shapes until it
                # migrates to "message", then delete these four lines.
                "message_id": payload["id"],
                "content": payload["content"],
                "sender": payload["sender"],
                "created_at": payload["created_at"],
            }
        )

        # Notify participants for conversation list update
        # We also notify the sender so their list updates correctly on other devices
        participants = ConversationParticipant.objects.filter(conversation=conversation)
        for participant in participants:
            user_id = participant.user_id if participant.user else None
            org_id = participant.org_id if participant.org else None
            recipient_id = user_id or org_id
            if recipient_id:
                async_to_sync(channel_layer.group_send)(
                    f"user_notifications_{recipient_id}",
                    {
                        "type": "notification_message",
                        "notification_type": "conversation_updated",
                        "conversation_id": str(conversation.id),
                    }
                )

    # ----------------------------------------
    # PUSH NOTIFICATION
    # ----------------------------------------
    @staticmethod
    def _trigger_push(conversation, message):
        participants = ConversationParticipant.objects.filter(
            conversation=conversation
        ).select_related("user", "org")

        if message.sender_user:
            participants = participants.exclude(user=message.sender_user)
        elif message.sender_org:
            participants = participants.exclude(org=message.sender_org)

        for participant in participants:
            if message.message_type in Message.SHARED_TYPES:
                # Shares go through the notifications module: it writes the
                # in-app row (grouped per conversation, deduped per message)
                # and sends the push itself.
                NotificationService.message_share(
                    message,
                    recipient_user=participant.user,
                    recipient_org=participant.org,
                )
                continue

            # Text/media keep the existing push-only behaviour — no in-app
            # notification row is written for ordinary chat.
            if participant.user:
                # Caption if there is one, else a media-type-specific line.
                if message.content:
                    body = message.content[:50]
                elif message.message_type == Message.Type.IMAGE:
                    body = "📷 Sent you a photo"
                elif message.message_type == Message.Type.VIDEO:
                    body = "🎥 Sent you a video"
                else:
                    body = ""

                FCMService.send_to_user(
                    participant.user,
                    {
                        "type": "message",
                        "title": "New message",
                        "body": body,
                        "conversation_id": str(conversation.id),
                        "sender_name": message.sender_user.profile_name
                        if message.sender_user else "",
                        "url": f"/messages/{conversation.id}"
                    }
                )
