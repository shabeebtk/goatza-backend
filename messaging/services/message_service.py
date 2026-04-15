'''
1. Send message
2. Validate sender
3. Handle request logic
4. Save message
5. Update conversation
6. Trigger async events (WebSocket + FCM)
'''


from django.utils import timezone
from django.db import transaction
from asgiref.sync import async_to_sync

from messaging.models import Message, Conversation, ConversationParticipant
from notifications.services.fcm_service import FCMService


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

        # VALIDATION
        MessageService._validate_sender(conversation, sender_user, sender_org)

        # TRANSACTION 
        with transaction.atomic():

            message = MessageService._create_message(
                conversation,
                sender_user,
                sender_org,
                content,
                message_type
            )

            MessageService._update_conversation(conversation, message)

        # REALTIME + PUSH
        MessageService._trigger_realtime(conversation, message)
        MessageService._trigger_push(conversation, message)

        return message

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
            raise Exception("Invalid sender")

        if not query.exists():
            raise Exception("Sender not part of conversation")


    # CREATE MESSAGE
    @staticmethod
    def _create_message(conversation, sender_user, sender_org, content, message_type):
        return Message.objects.create(
            conversation=conversation,
            sender_user=sender_user,
            sender_org=sender_org,
            content=content,
            message_type=message_type,
        )

    # UPDATE CONVERSATION
    @staticmethod
    def _update_conversation(conversation, message):
        conversation.last_message = message
        conversation.last_message_at = timezone.now()
        conversation.save(update_fields=["last_message", "last_message_at"])


    # REALTIME (WebSocket)
    @staticmethod
    def _trigger_realtime(conversation, message):
        from channels.layers import get_channel_layer
        from messaging.models import ConversationParticipant

        channel_layer = get_channel_layer()

        # Chat room routing
        async_to_sync(channel_layer.group_send)(
            f"chat_{conversation.id}",
            {
                "type": "chat_message",
                "message_id": str(message.id),
                "content": message.content,
                "sender_id": str(message.sender_user_id or message.sender_org_id),
                "created_at": message.created_at.isoformat(),
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

    # PUSH NOTIFICATION
    @staticmethod
    def _trigger_push(conversation, message):
        participants = ConversationParticipant.objects.filter(
            conversation=conversation
        )
        if message.sender_user:
            participants = participants.exclude(user=message.sender_user)
        elif message.sender_org:
            participants = participants.exclude(org=message.sender_org)

        for participant in participants:
            if participant.user:
                FCMService.send_to_user(
                    participant.user,
                    {
                        "type": "message",
                        "title": "New message",
                        "body": message.content[:50],
                        "conversation_id": str(conversation.id),
                        "sender_name": message.sender_user.profile_name
                        if message.sender_user else "",
                        "url": f"/messages/{conversation.id}"
                    }
                )