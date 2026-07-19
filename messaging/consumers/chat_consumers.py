import json
from channels.generic.websocket import AsyncWebsocketConsumer
from asgiref.sync import sync_to_async

from messaging.models import Conversation, ConversationParticipant
from messaging.services.message_service import MessageService


class ChatConsumer(AsyncWebsocketConsumer):

    # CONNECT
    async def connect(self):
        self.user = self.scope["user"]
        self.actor = self.scope.get("actor")  # setup in auth websocket 

        if not self.user or self.user.is_anonymous:
            await self.close()
            return

        self.conversation_id = self.scope["url_route"]["kwargs"]["conversation_id"]
        self.room_group_name = f"chat_{self.conversation_id}"

        is_allowed = await self._is_participant()   

        if not is_allowed:
            await self.close()
            return

        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        await self.accept(subprotocol="access_token")

    # DISCONNECT
    async def disconnect(self, close_code):
        if hasattr(self, "room_group_name"):
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name
            )

    # RECEIVE MESSAGE FROM CLIENT
    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
            message_text = data.get("message")

            if not message_text:
                return

            conversation = await sync_to_async(Conversation.objects.get)(
                id=self.conversation_id
            )

            await sync_to_async(MessageService.send_message)(
                conversation=conversation,
                sender_user=self.actor.user if self.actor.is_user else None,
                sender_org=self.actor.organization if self.actor.is_org else None,
                content=message_text
            )

        except Exception as e:
            await self.send(json.dumps({
                "type": "error",
                "message": str(e)
            }))

       
    # RECEIVE FROM GROUP (Redis)
    async def chat_message(self, event):
        """
        This is triggered by:
        channel_layer.group_send()

        Emits the full serialized message under "message" — the same shape the
        REST list endpoint returns, so the client has one message schema.

        The legacy flat keys (message_id/content/sender/created_at) are echoed
        alongside it because the current web client still reads those. Drop them
        once useChatSocket.ts consumes "message".
        """
        message = event["message"]

        # The payload was rendered without a viewer, so any followers-only share
        # came back "unavailable". This socket knows who it belongs to — if the
        # share is one THIS actor may see, re-render it for them. Costs a query,
        # but only for shares that were hidden, never for ordinary chat.
        if self._has_hidden_share(message):
            message = await self._serialize_for_me(message["id"]) or message

        await self.send(text_data=json.dumps({
            "type": "message",
            "message": message,

            # DEPRECATED — see docstring.
            "message_id": event["message_id"],
            "content": event["content"],
            "sender": event["sender"],
            "created_at": event["created_at"],
        }, default=str))

    @staticmethod
    def _has_hidden_share(message):
        return any(
            (message.get(key) or {}).get("unavailable")
            for key in ("shared_post_preview", "shared_recruitment_preview")
        )

    @sync_to_async
    def _serialize_for_me(self, message_id):
        from messaging.selectors.message_selectors import MessageSelector
        from messaging.serializers.message_serializers import MessageSerializer
        from messaging.selectors.share_selectors import ShareViewer

        message = MessageSelector.list_messages(
            self.conversation_id
        ).filter(id=message_id).first()

        if not message:
            return None

        return MessageSerializer(
            message,
            context={"viewer": ShareViewer.from_actor(self.actor)},
        ).data

    # VALIDATION
    @sync_to_async
    def _is_participant(self):
        return ConversationParticipant.objects.filter(
            conversation_id=self.conversation_id,
            user=self.actor.user if self.actor.is_user else None,
            org=self.actor.organization if self.actor.is_org else None
        ).exists()

