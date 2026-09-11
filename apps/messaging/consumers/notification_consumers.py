import json
from channels.generic.websocket import AsyncWebsocketConsumer


class UserNotificationsConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = self.scope.get("user")
        self.actor = self.scope.get("actor")

        if not self.user or self.user.is_anonymous:
            await self.close()
            return
            
        recipient_id = self.actor.organization.id if self.actor and self.actor.is_org else self.user.id
        self.room_group_name = f"user_notifications_{recipient_id}"
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