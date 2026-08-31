import json
from channels.generic.websocket import AsyncWebsocketConsumer
from asgiref.sync import sync_to_async

from legal.permissions import TERMS_REQUIRED_CODE, TERMS_REQUIRED_MESSAGE
from legal.selectors.acceptance_selectors import get_pending_documents
from messaging.models import Conversation, ConversationParticipant
from messaging.services.exceptions import BlockedParticipantError
from messaging.services.message_service import MessageService
from moderation.services.block_guard import BLOCKED_MESSAGE

# Application close code for "you have not accepted the current terms". The
# 4000-4999 range is reserved for the application, and 4403 is chosen to read
# as the 403 the REST half of the gate returns — the client branches on it to
# raise the same re-consent modal instead of retrying the socket forever.
WS_CLOSE_TERMS_REQUIRED = 4403


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

        # The chat socket exists to SEND. Unlike the REST surface, where the
        # gate can let reads through and refuse writes on the same connection,
        # there is one socket here and its purpose is the write half — so a
        # gated user is refused it and reads history over REST instead, where
        # GET is never gated. The notifications socket is untouched: it is
        # server-to-client only, so there is nothing there to gate.
        if await self._has_pending_documents():
            await self.close(code=WS_CLOSE_TERMS_REQUIRED)
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

            # Checked again per message, not only on connect. A socket opened
            # before a version bump stays open across it, and a connect-time
            # check alone would let exactly the long-lived sessions we most
            # want to stop keep writing until they happen to reconnect.
            pending = await self._has_pending_documents()
            if pending:
                await self.send(json.dumps({
                    "type": "error",
                    "code": TERMS_REQUIRED_CODE,
                    "message": TERMS_REQUIRED_MESSAGE,
                    "pending_documents": pending,
                }))
                await self.close(code=WS_CLOSE_TERMS_REQUIRED)
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

        except BlockedParticipantError:
            # NOT DELIVERED, and nothing is written. The socket stays open and
            # the sender gets the same vague wording every other block guard
            # uses — a closed socket or a specific reason would both tell them
            # they were blocked. Caught ahead of the generic handler below so
            # the machine-readable code travels with it.
            await self.send(json.dumps({
                "type": "error",
                "code": BlockedParticipantError.reason,
                "message": BLOCKED_MESSAGE,
            }))

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

    async def message_deleted(self, event):
        """
        A message was unsent. Only the id travels — clients drop it from their
        cache; there is nothing left to render.
        """
        await self.send(text_data=json.dumps({
            "type": "message_deleted",
            "message_id": event["message_id"],
        }))

    async def conversation_read(self, event):
        """
        Someone read the thread up to ``last_read_at``.

        A watermark, not a list of message ids: one timestamp flips every
        bubble at or before it, so a reader catching up on fifty messages costs
        the same single event as one.

        Sent to the whole room, including the reader's own other devices —
        clients tell the two apart by comparing ``reader_id`` to their actor.
        """
        await self.send(text_data=json.dumps({
            "type": "conversation_read",
            "reader_id": event["reader_id"],
            "last_read_at": event["last_read_at"],
        }))

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

    @sync_to_async
    def _has_pending_documents(self):
        """
        The documents this socket's USER still owes, or [].

        Always the user, never the actor: consent is given by a person, and an
        org actor is that same person wearing a different hat. Refreshed from
        the database rather than read off self.user, because the scope's user
        was loaded when the socket opened and a socket can outlive both a
        version bump and the acceptance that clears it.
        """
        self.user.refresh_from_db(
            fields=["terms_version", "privacy_version"]
        )
        return get_pending_documents(self.user)

