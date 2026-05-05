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
        """

        await self.send(text_data=json.dumps({
            "type": "message",
            "message_id": event["message_id"],
            "content": event["content"],
            "sender": event["sender"],
            "created_at": event["created_at"],
        }))

    # VALIDATION
    @sync_to_async
    def _is_participant(self):
        return ConversationParticipant.objects.filter(
            conversation_id=self.conversation_id,
            user=self.actor.user if self.actor.is_user else None,
            org=self.actor.organization if self.actor.is_org else None
        ).exists()

