import json
from channels.generic.websocket import AsyncWebsocketConsumer

from utils.realtime import WS_CLOSE_ORG_ACCESS_REVOKED, org_actor_sockets_group


class UserNotificationsConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = self.scope.get("user")
        self.actor = self.scope.get("actor")

        if not self.user or self.user.is_anonymous:
            await self.close()
            return

        # A logged-in user with no actor asked to act as an org and was refused
        # (not a member any more, or the org is suspended). Falling back to their
        # personal group would hide that from the client, so it is refused too.
        if self.actor is None:
            await self.close()
            return

        recipient_id = self.actor.organization.id if self.actor.is_org else self.user.id
        self.room_group_name = f"user_notifications_{recipient_id}"
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        if self.actor.is_org:
            self.org_actor_group = org_actor_sockets_group(self.actor.organization.id)
            await self.channel_layer.group_add(
                self.org_actor_group,
                self.channel_name
            )

        await self.accept(subprotocol="access_token")

    async def disconnect(self, close_code):
        if hasattr(self, "room_group_name"):
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name
            )

        if hasattr(self, "org_actor_group"):
            await self.channel_layer.group_discard(
                self.org_actor_group,
                self.channel_name
            )

    async def notification_message(self, event):
        await self.send(text_data=json.dumps(event))

    async def org_member_changed(self, event):
        """
        One member's membership changed, announced to everyone acting as the org.

        Only that member's sockets answer: they get the same frame a personal
        socket would, then close if access is gone. The frame goes first because
        a phone acting as the org hears about its removal nowhere else.
        """
        if event["user_id"] != str(self.user.id):
            return

        await self.send(text_data=json.dumps({
            "type": "notification_message",
            "notification_type": event["notification_type"],
            "organization_id": event["organization_id"],
            "status": event["status"],
            "role": event["role"],
        }))

        if event["close"]:
            await self.close(code=WS_CLOSE_ORG_ACCESS_REVOKED)
