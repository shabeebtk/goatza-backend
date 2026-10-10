"""
A dead Redis must cost the nudge, never the message.

WHAT BROKE BEFORE. ``_trigger_realtime`` called ``group_send`` bare. The
comment above it said a dead Redis must not roll back the message, and it was
right that the row survived — it was committed first. But the exception still
escaped, the view answered 500, and the SENDER was told their delivered
message had failed. So they sent it again. One dead Redis produced duplicate
messages in a chat that looked broken while every message was safely stored.

``utils.realtime.safe_group_send`` is the fix and its contract is that it never
raises. The load-bearing test here is
``test_a_dead_channel_layer_still_returns_201_and_stores_the_message``:
everything else can be re-derived from the helper, but that one is the bug.
"""

import uuid
from unittest.mock import patch

from django.conf import settings
from django.core.cache import cache
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User, UserProfile
from apps.connections.models import Follow
from apps.legal.testing import accept_current_terms
from apps.messaging.models import Message
from apps.messaging.services.conversation_service import ConversationService
from apps.messaging.tasks import push_message
from utils.realtime import safe_group_send

IN_MEMORY = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}

# The group_send the services reach, patched at the one place that calls it.
GROUP_SEND = "utils.realtime.async_to_sync"


class SafeGroupSendTests(APITestCase):
    """The helper itself."""

    @override_settings(CHANNEL_LAYERS=IN_MEMORY)
    def test_a_working_layer_returns_true_and_logs_nothing(self):
        with patch("utils.realtime.logger") as logger:
            sent = safe_group_send("chat_x", {"type": "chat_message"}, tag="t")

        self.assertTrue(sent)
        logger.error.assert_not_called()
        logger.warning.assert_not_called()

    @override_settings(CHANNEL_LAYERS=IN_MEMORY)
    def test_a_raising_layer_returns_false_and_logs_an_error(self):
        # ERROR, so the Sentry integration raises an event: the request is
        # about to succeed and nothing else will ever mention this.
        with patch(GROUP_SEND, side_effect=ConnectionError("redis is gone")):
            with self.assertLogs("utils.realtime", level="ERROR") as logs:
                sent = safe_group_send(
                    "chat_x", {"type": "chat_message"}, tag="messaging.chat_message"
                )

        self.assertFalse(sent)
        # The tag is what makes a failure say WHICH fan-out was lost.
        self.assertIn("messaging.chat_message", logs.output[0])
        self.assertIn("group=chat_x", logs.output[0])

    @override_settings(CHANNEL_LAYERS={})
    def test_no_configured_layer_is_a_warning_not_an_error(self):
        # Nothing is running, so nothing is listening either — not a failure.
        with self.assertLogs("utils.realtime", level="WARNING") as logs:
            sent = safe_group_send("chat_x", {"type": "x"}, tag="t")

        self.assertFalse(sent)
        self.assertIn("no channel layer", logs.output[0])

    @override_settings(CHANNEL_LAYERS=IN_MEMORY)
    def test_the_tag_is_keyword_only(self):
        # Passed positionally once, in the wrong order, it would lie in the
        # logs forever.
        with self.assertRaises(TypeError):
            safe_group_send("chat_x", {"type": "x"}, "messaging.chat_message")


@override_settings(CHANNEL_LAYERS=IN_MEMORY)
class ChatSendSurvivesDeadRedisTests(APITestCase):
    """
    The end-to-end version, through the real endpoint.

    The media endpoint is used because it is the one that returns 201 for a
    sent chat message; text goes over the websocket consumer. The URL has to
    pass the "never trust the client URL" checks, so it is built from
    MEDIA_PUBLIC_BASE_URL and the sender's own chat prefix.
    """

    def setUp(self):
        # Throttle state lives in the default cache and leaks between tests.
        cache.clear()

        self.sender = self._user("sender")
        self.receiver = self._user("receiver")

        Follow.objects.get_or_create(
            follower_user=self.sender, following_user=self.receiver
        )
        Follow.objects.get_or_create(
            follower_user=self.receiver, following_user=self.sender
        )
        self.conversation, _ = ConversationService.get_or_create_conversation(
            actor_user=self.sender, target_user=self.receiver
        )
        self.client.force_authenticate(user=self.sender)

    def _user(self, username):
        user = User.objects.create_user(
            email=f"{username}@example.com", password="pass1234", username=username,
        )
        accept_current_terms(user)
        UserProfile.objects.create(user=user, name=username.title())
        return user

    def _media_body(self):
        key = f"chat/users/{self.sender.id}/{uuid.uuid4()}.jpg"
        return {
            "media_type": "image",
            "media_url": f"{settings.MEDIA_PUBLIC_BASE_URL}/{key}",
            "media_public_id": key,
            "caption": "look at this",
        }

    def _url(self):
        return f"/conversations/{self.conversation.id}/messages/media"

    def test_a_healthy_send_is_201(self):
        with patch("apps.messaging.tasks.FCMService.send_to_user"):
            with self.captureOnCommitCallbacks(execute=True):
                res = self.client.post(self._url(), self._media_body(), format="json")

        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Message.objects.count(), 1)

    def test_a_dead_channel_layer_still_returns_201_and_stores_the_message(self):
        """
        THE BUG. A raising group_send used to escape the view as a 500, so the
        sender re-sent a message that had already been delivered.
        """
        with patch(GROUP_SEND, side_effect=ConnectionError("redis is gone")):
            with patch("apps.messaging.tasks.FCMService.send_to_user"):
                with self.assertLogs("utils.realtime", level="ERROR"):
                    with self.captureOnCommitCallbacks(execute=True):
                        res = self.client.post(
                            self._url(), self._media_body(), format="json"
                        )

        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Message.objects.count(), 1)

    def test_the_push_is_dispatched_as_an_id_through_enqueue(self):
        with patch("apps.messaging.services.message_service.enqueue") as enqueue:
            res = self.client.post(self._url(), self._media_body(), format="json")

        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        message = Message.objects.get()

        enqueue.assert_called_once()
        task, args = enqueue.call_args.args
        self.assertEqual(task.name, "messaging.push_message")
        self.assertEqual(args, (str(message.id),))

    def test_a_dead_fcm_does_not_fail_the_send_either(self):
        with patch(
            "apps.messaging.tasks.FCMService.send_to_user",
            side_effect=ConnectionError("fcm is gone"),
        ):
            with self.assertLogs("apps.messaging.tasks", level="WARNING"):
                with self.captureOnCommitCallbacks(execute=True):
                    res = self.client.post(
                        self._url(), self._media_body(), format="json"
                    )

        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Message.objects.count(), 1)


class PushMessageTaskTests(APITestCase):
    """The chat push task on its own."""

    def test_a_deleted_message_returns_quietly(self):
        # Somebody unsending a message they just sent is the common case here,
        # not a fault — INFO and return.
        with patch("apps.messaging.tasks.FCMService.send_to_user") as send:
            with self.assertLogs("apps.messaging.tasks", level="INFO") as logs:
                result = push_message(str(uuid.uuid4()))

        self.assertIsNone(result)
        send.assert_not_called()
        self.assertIn("message gone", logs.output[0])

    def test_acks_late_is_off_so_a_crash_cannot_re_notify_the_conversation(self):
        self.assertFalse(push_message.acks_late)
