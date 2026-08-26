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
    is_org_profile_shareable,
    is_post_shareable,
    is_recruitment_shareable,
    is_user_profile_shareable,
)
from messaging.services.exceptions import (
    BlockedParticipantError,
    ContentUnavailableError,
    EmptyMessageError,
    InvalidMediaError,
    InvalidSenderError,
    MessageNotFoundError,
    NotMessageSenderError,
    NotParticipantError,
)
from feed.services.affinity_services import AffinityService
from moderation.services.block_guard import require_not_blocked
from notifications.services.deeplink_service import build_conversation_url
from notifications.services.fcm_service import FCMService
from notifications.services.notification_service import (
    NotificationService,
    get_org_admin_users,
)
from services.storage.metadata import MAX_DIMENSION, clamp_int
from services.storage.validators import (
    extract_storage_key,
    get_file_extension,
    is_valid_media_source,
    same_storage_folder,
)

# Chat images. No "heic": the stored object is the byte-for-byte file the
# browser uploaded and nothing transcodes it on delivery, so a .heic bubble
# would store fine and then render as nothing.
CHAT_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
CHAT_IMAGE_MAX_BYTES = 10 * 1024 * 1024  # 10 MB

# Chat videos — the two containers the client-side encoder emits. No "mov", for
# the same reason.
CHAT_VIDEO_EXTENSIONS = {"mp4", "webm"}
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

    # SHARE A USER PROFILE
    @staticmethod
    def send_shared_user_profile(
        conversation: Conversation,
        sender_user=None,
        sender_org=None,
        profile_user=None,
        note: str = "",
    ):
        """
        Forward a person's profile into a conversation. ``note`` is an optional
        caption and is stored as the message content.
        """
        viewer = ShareViewer(user=sender_user, org=sender_org)

        if not is_user_profile_shareable(profile_user, viewer):
            raise ContentUnavailableError("This profile is no longer available")

        return MessageService._create_and_dispatch(
            conversation,
            sender_user,
            sender_org,
            message_type=Message.Type.SHARED_USER_PROFILE,
            content=note or "",
            shared_profile_user=profile_user,
        )

    # SHARE AN ORGANIZATION PROFILE
    @staticmethod
    def send_shared_org_profile(
        conversation: Conversation,
        sender_user=None,
        sender_org=None,
        profile_org=None,
        note: str = "",
    ):
        """
        Forward an organization's profile into a conversation. ``note`` is an
        optional caption and is stored as the message content.
        """
        viewer = ShareViewer(user=sender_user, org=sender_org)

        if not is_org_profile_shareable(profile_org, viewer):
            raise ContentUnavailableError("This profile is no longer available")

        return MessageService._create_and_dispatch(
            conversation,
            sender_user,
            sender_org,
            message_type=Message.Type.SHARED_ORG_PROFILE,
            content=note or "",
            shared_profile_org=profile_org,
        )

    # SEND AN IMAGE
    @staticmethod
    def send_image_message(
        conversation: Conversation,
        sender_user=None,
        sender_org=None,
        media_url: str = "",
        media_public_id: str = "",
        thumbnail_url: str = "",
        width=None,
        height=None,
        size_bytes=None,
        caption: str = "",
    ):
        """
        Send a photo message. The media has already been uploaded straight to
        storage by the client (presigned upload) — we get the URL back, so we
        must re-validate it belongs to us before trusting it (see
        _validate_chat_image). width/height are client-reported and only drive
        layout, so they are range-checked and otherwise stored as sent.

        ``thumbnail_url`` is optional for an image (a cheap preview for the
        list); absent, the column stays blank as it always has.
        """
        MessageService._validate_chat_image(
            sender_user, sender_org, media_url, media_public_id, size_bytes
        )

        thumbnail_url = (thumbnail_url or "").strip()
        if thumbnail_url:
            MessageService._validate_chat_thumbnail(
                sender_user, sender_org, thumbnail_url, media_public_id
            )

        width = clamp_int(width, maximum=MAX_DIMENSION)
        height = clamp_int(height, maximum=MAX_DIMENSION)

        return MessageService._create_and_dispatch(
            conversation,
            sender_user,
            sender_org,
            message_type=Message.Type.IMAGE,
            content=caption or "",
            media_url=media_url,
            media_public_id=media_public_id,
            media_thumbnail_url=thumbnail_url,
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
        thumbnail_url: str = "",
        width=None,
        height=None,
        duration_ms=None,
        size_bytes=None,
        caption: str = "",
    ):
        """
        Send a video message. Like send_image_message, but validates the video
        constraints (format/size/duration).

        The poster frame now comes from the CLIENT: nothing derives one
        server-side any more, so ``thumbnail_url`` is required and is put
        through the same checks as the clip itself, plus a same-folder rule
        (see _validate_chat_thumbnail).
        """
        MessageService._validate_chat_video(
            sender_user, sender_org, media_url, media_public_id,
            size_bytes, duration_ms,
        )

        thumbnail_url = (thumbnail_url or "").strip()
        if not thumbnail_url:
            raise InvalidMediaError("Video thumbnail is required")

        MessageService._validate_chat_thumbnail(
            sender_user, sender_org, thumbnail_url, media_public_id
        )

        width = clamp_int(width, maximum=MAX_DIMENSION)
        height = clamp_int(height, maximum=MAX_DIMENSION)

        return MessageService._create_and_dispatch(
            conversation,
            sender_user,
            sender_org,
            message_type=Message.Type.VIDEO,
            content=caption or "",
            media_url=media_url,
            media_public_id=media_public_id,
            media_thumbnail_url=thumbnail_url,
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
    def _chat_extensions(kind: str):
        """The extension allowlist for a chat image or video."""
        return (
            CHAT_IMAGE_EXTENSIONS if kind == "image"
            else CHAT_VIDEO_EXTENSIONS
        )

    @staticmethod
    def _validate_chat_media_url(
        sender_user, sender_org, media_url, media_public_id, allowed_extensions
    ):
        """
        Shared "never trust the client URL" checks for image + video. Accepted
        only when it points at OUR storage, has an allowed extension, lives under
        the SENDER's own chat folder (so another actor's URL can't be replayed),
        and the key embedded in the URL matches the one sent.

        The prefix check is the replay protection: a URL is only ever accepted
        under chat/users/<sender>/ or chat/organizations/<sender>/.
        """
        if not media_url or not is_valid_media_source(media_url):
            raise InvalidMediaError("Invalid media source")

        if get_file_extension(media_url) not in allowed_extensions:
            raise InvalidMediaError("Unsupported media format")

        prefix = MessageService._chat_media_prefix(sender_user, sender_org)
        if not media_public_id or not media_public_id.startswith(prefix):
            raise InvalidMediaError("Invalid media path")

        if extract_storage_key(media_url) != media_public_id:
            raise InvalidMediaError("Media URL and public_id mismatch")

    @staticmethod
    def _validate_chat_thumbnail(
        sender_user, sender_org, thumbnail_url, media_public_id
    ):
        """
        A client-supplied poster frame, held to the same bar as the media it
        belongs to — nothing derives one server-side any more.

        On top of the shared checks (our storage, image extension, the SENDER's
        own chat prefix, URL↔key match) the thumbnail must live in the SAME
        FOLDER as the video. Without that, a sender could pair a clip with a
        poster lifted from any other message they ever sent.
        """
        MessageService._validate_chat_media_url(
            sender_user,
            sender_org,
            thumbnail_url,
            extract_storage_key(thumbnail_url),
            MessageService._chat_extensions("image"),
        )

        if not same_storage_folder(
            extract_storage_key(thumbnail_url), media_public_id
        ):
            raise InvalidMediaError("Invalid media path")

        return thumbnail_url

    @staticmethod
    def _validate_chat_image(
        sender_user, sender_org, media_url, media_public_id, size_bytes
    ):
        MessageService._validate_chat_media_url(
            sender_user, sender_org, media_url, media_public_id,
            MessageService._chat_extensions("image"),
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
            MessageService._chat_extensions("video"),
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
        MessageService._validate_not_blocked(conversation, sender_user, sender_org)

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
        MessageService._record_affinity(conversation, message)

        return message

    @staticmethod
    def _record_affinity(conversation, message):
        """
        §3.6 — messaging someone is the strongest signal in the model (+5), so
        their posts float up the sender's feed.

        Only the SENDER's affinity moves: they chose to reach out; the recipient
        chose nothing. Org senders are skipped — affinity is keyed to a person
        and ``_create_and_dispatch`` only knows which org sent, not which member.
        """
        if not message.sender_user_id:
            return

        participants = (
            ConversationParticipant.objects
            .filter(conversation=conversation)
            .values_list("user_id", "org_id")
        )

        for user_id, org_id in participants:
            # The sender is skipped by AffinityService itself (no self-affinity);
            # filtering them out in SQL would need an IS NULL guard for the org
            # participants, which is more fragile than just letting it through.
            AffinityService.record(
                message.sender_user,
                AffinityService.MESSAGE,
                author_user_id=user_id,
                author_org_id=org_id,
            )

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

    # BLOCK GUARD
    @staticmethod
    def _validate_not_blocked(conversation, sender_user, sender_org):
        """
        Refuse the send if a block exists between the sender and anyone else in
        the thread, in either direction.

        Deliberately in _create_and_dispatch rather than in send_message: text,
        media and every send_shared_* funnel through that one method, so this
        covers the WebSocket path, the media endpoint and the share endpoint
        with a single check that no future sender can forget.

        Raises BlockedParticipantError (a MessageError), so a blocked recipient
        in a multi-target share is reported against that recipient and the rest
        of the fan-out still lands.
        """
        sender = sender_user or sender_org

        others = ConversationParticipant.objects.filter(
            conversation=conversation
        ).select_related("user", "org")

        for participant in others:
            other = participant.user or participant.org

            if other is None or other == sender:
                continue

            require_not_blocked(
                sender, other, error=BlockedParticipantError
            )

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
                FCMService.send_to_user(target, payload)
