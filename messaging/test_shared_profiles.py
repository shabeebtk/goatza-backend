"""
Forwarding a profile into a chat.

A separate module rather than more of ``messaging/tests.py``: that file is
already 2,000 lines and its fixtures are exactly what this needs, so it is
imported instead of duplicated. The runner picks up both (``test*.py``).

The rule worth stating up front, because it looks like an omission: profile
shares deliberately do NOT check ``is_public_profile``. That flag governs the
logged-out web view only. Inside Goatza every signed-in actor can already see
every profile, so a hidden profile stays shareable and its card renders
normally — refusing would take away a capability the recipient already has and
would leak the owner's privacy setting to whoever tried.
"""

import uuid

from django.db import IntegrityError, transaction
from django.test import override_settings
from rest_framework import status

from accounts.models import User, UserProfile
from messaging.models import Conversation, Message
from messaging.selectors.share_selectors import (
    ShareViewer,
    is_org_profile_shareable,
    is_user_profile_shareable,
)
from messaging.serializers.message_serializers import MessageSerializer
from messaging.services.message_service import MessageService
from messaging.tests import (
    CHANNEL_LAYERS,
    MESSAGES_URL,
    SHARE_URL,
    MessagingShareTestCase,
)
from notifications.models import Notification
from posts.models import Post


@override_settings(CHANNEL_LAYERS=CHANNEL_LAYERS)
class ShareProfileTests(MessagingShareTestCase):

    def test_share_user_profile(self):
        conversation = self._mutual_conversation()

        res = self.client.post(
            SHARE_URL,
            self._share_body(
                target_type="user",
                target_id=self.author.id,
                conversation_ids=[str(conversation.id)],
            ),
            format="json",
        )

        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["data"]["sent"], [str(conversation.id)])

        message = Message.objects.get(conversation=conversation)
        self.assertEqual(message.message_type, Message.Type.SHARED_USER_PROFILE)
        self.assertEqual(message.shared_profile_user_id, self.author.id)
        self.assertIsNone(message.shared_post_id)
        self.assertIsNone(message.shared_recruitment_id)
        self.assertIsNone(message.shared_profile_org_id)

    def test_share_org_profile(self):
        conversation = self._mutual_conversation()

        res = self.client.post(
            SHARE_URL,
            self._share_body(
                target_type="organization",
                target_id=self.other_org.id,
                conversation_ids=[str(conversation.id)],
            ),
            format="json",
        )

        self.assertEqual(res.status_code, status.HTTP_200_OK)

        message = Message.objects.get(conversation=conversation)
        self.assertEqual(message.message_type, Message.Type.SHARED_ORG_PROFILE)
        self.assertEqual(message.shared_profile_org_id, self.other_org.id)

    def test_hidden_profile_is_still_shareable(self):
        """The whole point of the rule — see the module docstring."""
        self.author.profile.is_public_profile = False
        self.author.profile.save(update_fields=["is_public_profile"])

        conversation = self._mutual_conversation()

        res = self.client.post(
            SHARE_URL,
            self._share_body(
                target_type="user",
                target_id=self.author.id,
                conversation_ids=[str(conversation.id)],
            ),
            format="json",
        )

        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.data["data"]["sent"]), 1)

    def test_hidden_profile_preview_renders_normally(self):
        self.author.profile.is_public_profile = False
        self.author.profile.save(update_fields=["is_public_profile"])

        conversation = self._mutual_conversation()
        MessageService.send_shared_user_profile(
            conversation=conversation,
            sender_user=self.sender,
            profile_user=self.author,
        )

        self.client.force_authenticate(user=self.receiver)
        res = self.client.get(
            MESSAGES_URL, {"conversation_id": str(conversation.id)}
        )

        preview = res.data["data"]["results"][0]["shared_user_profile_preview"]
        self.assertFalse(preview["unavailable"])
        self.assertEqual(preview["username"], "author")

    def test_inactive_user_cannot_be_shared(self):
        self.author.is_active = False
        self.author.save(update_fields=["is_active"])

        conversation = self._mutual_conversation()

        res = self.client.post(
            SHARE_URL,
            self._share_body(
                target_type="user",
                target_id=self.author.id,
                conversation_ids=[str(conversation.id)],
            ),
            format="json",
        )

        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    def test_usernameless_user_cannot_be_shared(self):
        """No username means no profile URL — the card would open nothing."""
        nameless = User.objects.create_user(
            email="nameless@example.com", password="pass1234",
        )
        UserProfile.objects.create(user=nameless, name="Nameless")

        conversation = self._mutual_conversation()

        res = self.client.post(
            SHARE_URL,
            self._share_body(
                target_type="user",
                target_id=nameless.id,
                conversation_ids=[str(conversation.id)],
            ),
            format="json",
        )

        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    def test_inactive_org_cannot_be_shared(self):
        self.other_org.is_active = False
        self.other_org.save(update_fields=["is_active"])

        conversation = self._mutual_conversation()

        res = self.client.post(
            SHARE_URL,
            self._share_body(
                target_type="organization",
                target_id=self.other_org.id,
                conversation_ids=[str(conversation.id)],
            ),
            format="json",
        )

        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    def test_sharing_a_profile_never_touches_the_follow_graph(self):
        """
        ShareViewer._follows fires a query on first access. Profiles have no
        followers-only tier, so this path must never reach it — otherwise a
        10-target fan-out pays for an answer that cannot change the outcome.
        """
        viewer = ShareViewer(user=self.sender)

        with self.assertNumQueries(0):
            self.assertTrue(is_user_profile_shareable(self.author, viewer))
            self.assertTrue(is_org_profile_shareable(self.other_org, viewer))

        self.assertNotIn("_follows", viewer.__dict__)

    def test_deleting_the_target_nulls_the_fk_without_an_integrity_error(self):
        conversation = self._mutual_conversation()
        message = MessageService.send_shared_user_profile(
            conversation=conversation,
            sender_user=self.sender,
            profile_user=self.author,
        )

        self.author.delete()

        message.refresh_from_db()
        self.assertIsNone(message.shared_profile_user_id)
        self.assertEqual(message.message_type, Message.Type.SHARED_USER_PROFILE)

    def test_preview_is_unavailable_once_the_target_is_gone(self):
        conversation = self._mutual_conversation()
        MessageService.send_shared_user_profile(
            conversation=conversation,
            sender_user=self.sender,
            profile_user=self.author,
        )
        self.author.delete()

        self.client.force_authenticate(user=self.receiver)
        res = self.client.get(
            MESSAGES_URL, {"conversation_id": str(conversation.id)}
        )

        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(
            res.data["data"]["results"][0]["shared_user_profile_preview"],
            {"unavailable": True},
        )

    def test_preview_is_unavailable_once_the_target_deactivates(self):
        """The QA matrix's "recipient sees a deactivated profile" row."""
        conversation = self._mutual_conversation()
        MessageService.send_shared_user_profile(
            conversation=conversation,
            sender_user=self.sender,
            profile_user=self.author,
        )

        self.author.is_active = False
        self.author.save(update_fields=["is_active"])

        self.client.force_authenticate(user=self.receiver)
        res = self.client.get(
            MESSAGES_URL, {"conversation_id": str(conversation.id)}
        )

        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(
            res.data["data"]["results"][0]["shared_user_profile_preview"],
            {"unavailable": True},
        )

    def test_preview_values_are_all_primitives(self):
        """
        The same dict is msgpack'd onto the channel layer, which carries no
        Decimal, no UUID and no datetime.
        """
        conversation = self._mutual_conversation()
        MessageService.send_shared_user_profile(
            conversation=conversation,
            sender_user=self.sender,
            profile_user=self.author,
        )

        message = Message.objects.get(conversation=conversation)
        preview = MessageSerializer(
            message, context={"viewer": None}
        ).data["shared_user_profile_preview"]

        for key, value in preview.items():
            self.assertIsInstance(
                value, (str, int, float, bool),
                f"{key} is {type(value).__name__}, not a primitive",
            )

    def test_notification_names_the_right_kind(self):
        conversation = self._mutual_conversation()

        MessageService.send_shared_user_profile(
            conversation=conversation,
            sender_user=self.sender,
            profile_user=self.author,
            note="check them out",
        )

        notification = Notification.objects.get(recipient_user=self.receiver)
        self.assertEqual(notification.data["shared_kind"], "user_profile")
        self.assertEqual(notification.data["shared_id"], str(self.author.id))
        # Same reasoning as posts/recruitments: no content FK, because the
        # grouped list renders those without a visibility check.
        self.assertIsNone(notification.post_id)

    def test_org_notification_names_the_right_kind(self):
        conversation = self._mutual_conversation()

        MessageService.send_shared_org_profile(
            conversation=conversation,
            sender_user=self.sender,
            profile_org=self.other_org,
        )

        notification = Notification.objects.get(recipient_user=self.receiver)
        self.assertEqual(notification.data["shared_kind"], "org_profile")
        self.assertEqual(notification.data["shared_id"], str(self.other_org.id))

    def test_partial_success_across_several_targets(self):
        good = self._mutual_conversation()

        res = self.client.post(
            SHARE_URL,
            self._share_body(
                target_type="user",
                target_id=self.author.id,
                conversation_ids=[str(good.id), str(uuid.uuid4())],
            ),
            format="json",
        )

        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["data"]["sent"], [str(good.id)])
        self.assertEqual(len(res.data["data"]["failed"]), 1)
        self.assertEqual(
            res.data["data"]["failed"][0]["reason"], "conversation_not_found"
        )

    def test_share_to_a_new_recipient_lands_as_a_request(self):
        res = self.client.post(
            SHARE_URL,
            self._share_body(
                target_type="user",
                target_id=self.author.id,
                recipients=[
                    {"actor_type": "user", "actor_id": str(self.receiver.id)}
                ],
            ),
            format="json",
        )

        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.data["data"]["sent"]), 1)

        conversation = Conversation.objects.get(id=res.data["data"]["sent"][0])
        self.assertEqual(conversation.status, Conversation.Status.REQUESTED)
        self.assertFalse(
            conversation.participants.get(user=self.receiver).has_accepted
        )


class MessageSharedConstraintTests(MessagingShareTestCase):
    """
    message_shared_object_matches_type now covers four types and four FKs.
    Every branch asserts the other three are null, so a mislabeled row can
    never be written — while a SET_NULL that empties a column still passes.
    """

    def test_every_shared_type_inserts(self):
        conversation = self._mutual_conversation()

        valid = [
            (Message.Type.SHARED_POST, {"shared_post": self.post}),
            (Message.Type.SHARED_RECRUITMENT,
             {"shared_recruitment": self.recruitment}),
            (Message.Type.SHARED_USER_PROFILE,
             {"shared_profile_user": self.author}),
            (Message.Type.SHARED_ORG_PROFILE,
             {"shared_profile_org": self.other_org}),
        ]

        for message_type, fk in valid:
            with self.subTest(message_type=message_type):
                message = Message.objects.create(
                    conversation=conversation,
                    sender_user=self.sender,
                    message_type=message_type,
                    **fk,
                )
                self.assertIsNotNone(message.id)

    def test_a_text_message_with_no_shared_object_inserts(self):
        message = Message.objects.create(
            conversation=self._mutual_conversation(),
            sender_user=self.sender,
            message_type=Message.Type.TEXT,
            content="hello",
        )
        self.assertIsNotNone(message.id)

    def test_mislabeled_rows_are_rejected(self):
        conversation = self._mutual_conversation()

        mislabeled = [
            # Right FK, wrong type.
            (Message.Type.SHARED_POST, {"shared_profile_user": self.author}),
            (Message.Type.SHARED_USER_PROFILE, {"shared_post": self.post}),
            (Message.Type.SHARED_ORG_PROFILE,
             {"shared_profile_user": self.author}),
            # Two FKs at once.
            (Message.Type.SHARED_USER_PROFILE, {
                "shared_profile_user": self.author,
                "shared_profile_org": self.other_org,
            }),
            (Message.Type.SHARED_POST, {
                "shared_post": self.post,
                "shared_profile_org": self.other_org,
            }),
        ]

        for message_type, fields in mislabeled:
            with self.subTest(message_type=message_type, fields=list(fields)):
                with self.assertRaises(IntegrityError):
                    with transaction.atomic():
                        Message.objects.create(
                            conversation=conversation,
                            sender_user=self.sender,
                            message_type=message_type,
                            **fields,
                        )

    def test_deleting_a_shared_org_nulls_the_fk(self):
        conversation = self._mutual_conversation()
        message = Message.objects.create(
            conversation=conversation,
            sender_user=self.sender,
            message_type=Message.Type.SHARED_ORG_PROFILE,
            shared_profile_org=self.other_org,
        )

        # The forward constraint is deliberately absent — this delete would
        # abort with an IntegrityError if it were enforced.
        self.other_org.delete()

        message.refresh_from_db()
        self.assertIsNone(message.shared_profile_org_id)


@override_settings(CHANNEL_LAYERS=CHANNEL_LAYERS)
class SharedProfileQueryCountTests(MessagingShareTestCase):
    """
    The N+1 guard for shared profiles.

    A page holding all four share kinds costs a fixed number of queries. Both
    tests assert the SAME constant, so a regression that made the count scale
    with the page fails on the 8-set case while the 1-set case still passes —
    the same shape as MessageListQueryCountTests in messaging/tests.py.
    """

    #   1. participant/permission check
    #   2. the other participant's last_read_at (the read-receipt watermark)
    #   3. the message page (every select_related preview field rides on it)
    #   4. shared_post media prefetch
    #   5. shared_recruitment media prefetch
    #   6. shared_profile_user primary sport
    #   7. shared_profile_user primary position
    #   8. shared_profile_org locations
    EXPECTED_QUERIES = 8

    def _seed(self, sets, conversation):
        """`sets` × one message of each of the four shared types."""
        for i in range(sets):
            post = Post.objects.create(
                author_user=self.author,
                content=f"post {i}",
                visibility=Post.Visibility.PUBLIC,
            )
            MessageService.send_shared_post(
                conversation=conversation, sender_user=self.sender, post=post,
            )
            MessageService.send_shared_recruitment(
                conversation=conversation,
                sender_user=self.sender,
                recruitment=self.recruitment,
            )
            MessageService.send_shared_user_profile(
                conversation=conversation,
                sender_user=self.sender,
                profile_user=self.author,
            )
            MessageService.send_shared_org_profile(
                conversation=conversation,
                sender_user=self.sender,
                profile_org=self.other_org,
            )

    def _list_with_sets(self, sets):
        conversation = self._mutual_conversation()
        self._seed(sets, conversation)

        self.client.force_authenticate(user=self.receiver)

        with self.assertNumQueries(self.EXPECTED_QUERIES):
            response = self.client.get(
                MESSAGES_URL, {"conversation_id": str(conversation.id)}
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]["results"]), sets * 4)

    def test_one_set(self):
        self._list_with_sets(1)

    def test_four_sets_cost_the_same(self):
        """
        16 shared messages must cost exactly what 4 do. Four sets, not more:
        the list paginates at 20, and a page that truncated would compare the
        cost of two DIFFERENT page sizes and prove nothing.
        """
        self._list_with_sets(4)
