import json
from channels.generic.websocket import AsyncWebsocketConsumer
from asgiref.sync import sync_to_async

from messaging.models import Conversation, ConversationParticipant
from messaging.services.message_service import MessageService


class ChatConsumer(AsyncWebsocketConsumer):

    # CONNECT
    async def connect(self):
        self.user = self.scope["user"]

        # must be authenticated
        if not self.user or self.user.is_anonymous:
            await self.close()
            return

        self.conversation_id = self.scope["url_route"]["kwargs"]["conversation_id"]
        self.room_group_name = f"chat_{self.conversation_id}"

        # VALIDATE ACCESS
        is_allowed = await self._is_user_in_conversation()

        if not is_allowed:
            await self.close()
            return

        # JOIN GROUP
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
                sender_user=self.user,
                content=message_text
            )

            # MessageService already triggers:
            # - WebSocket broadcast
            # - FCM push


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
            "sender_id": event["sender_id"],
            "created_at": event["created_at"],
        }))

    # VALIDATION
    @sync_to_async
    def _is_user_in_conversation(self):
        exists = ConversationParticipant.objects.filter(
            conversation_id=self.conversation_id,
            user=self.user
        ).exists()
        print("IS ALLOWED:", exists)
        return exists

class UserNotificationsConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = self.scope.get("user")
        if not self.user or self.user.is_anonymous:
            await self.close()
            return
            
        self.room_group_name = f"user_notifications_{self.user.id}"
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )
        await self.accept(subprotocol="access_token")

    async def disconnect(self, close_code):
        if hasattr(self, "room_group_name"):
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name
            )

    async def notification_message(self, event):
        await self.send(text_data=json.dumps(event))