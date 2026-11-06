"""
Deleted or expired content never breaks a thread.

Every case the server promises: a card is whole or it is {"unavailable": true},
never half-filled and never a 500, and the reason is never given.
"""

from unittest.mock import patch

from django.core.cache import cache
from django.utils import timezone
from rest_framework import status

from apps.accounts.models import User
from apps.messaging.models import Conversation, ConversationParticipant, Message
from apps.messaging.selectors.share_selectors import (
    COUNTERPART_DELETED_MESSAGE,
    COUNTERPART_INACTIVE_MESSAGE,
    COUNTERPART_ORG_INACTIVE_MESSAGE,
)
from apps.messaging.serializers.message_serializers import DELETED_ACCOUNT_NAME
from apps.messaging.services.exceptions import CounterpartUnavailableError
from apps.messaging.services.message_service import MessageService
from apps.messaging.tests import MESSAGES_URL, SHARE_URL, MessagingShareTestCase
from apps.moderation.models import Block
from apps.organization.models import Organization
from apps.posts.models import Post
from apps.recruitments.models import Recruitment

UNAVAILABLE = {"unavailable": True}


class UnavailableBase(MessagingShareTestCase):
    def setUp(self):
        super().setUp()
        self.conversation = self._mutual_conversation()

    def _share(self, target_type, target_id):
        response = self.client.post(
            SHARE_URL,
            self._share_body(
                target_type, target_id, conversation_ids=[str(self.conversation.id)]
            ),
            format="json",
        )
        self.assertEqual(response.data["data"]["sent"], [str(self.conversation.id)])

    def _rows(self):
        response = self.client.get(
            MESSAGES_URL, {"conversation_id": str(self.conversation.id)}
        )
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]["results"]

    def _preview(self, key):
        return self._rows()[0][key]

    def _deactivate(self, user):
        User.objects.filter(pk=user.pk).update(is_active=False)

    def _delete_account(self, user):
        User.objects.filter(pk=user.pk).update(
            is_active=False, deletion_requested_at=timezone.now()
        )

    def _block(self, **ends):
        Block.objects.create(**ends)
        cache.clear()


class SharedPostCases(UnavailableBase):
    KEY = "shared_post_preview"

    def setUp(self):
        super().setUp()
        self._share("post", self.post.id)

    def test_whole_when_nothing_is_wrong(self):
        preview = self._preview(self.KEY)

        self.assertFalse(preview["unavailable"])
        self.assertEqual(preview["id"], str(self.post.id))

    def test_soft_deleted(self):
        Post.objects.filter(id=self.post.id).update(is_deleted=True)

        self.assertEqual(self._preview(self.KEY), UNAVAILABLE)

    def test_hard_deleted(self):
        self.post.delete()

        self.assertEqual(self._preview(self.KEY), UNAVAILABLE)

    def test_author_deactivated(self):
        self._deactivate(self.author)

        self.assertEqual(self._preview(self.KEY), UNAVAILABLE)

    def test_author_deleted_their_account(self):
        self._delete_account(self.author)

        self.assertEqual(self._preview(self.KEY), UNAVAILABLE)

    def test_followers_only_to_someone_who_does_not_follow(self):
        Post.objects.filter(id=self.post.id).update(
            visibility=Post.Visibility.FOLLOWERS
        )

        self.assertEqual(self._preview(self.KEY), UNAVAILABLE)

    def test_a_block_in_either_direction(self):
        for name, ends in (
            ("author blocked viewer", {"blocker_user": self.author, "blocked_user": self.sender}),
            ("viewer blocked author", {"blocker_user": self.sender, "blocked_user": self.author}),
        ):
            with self.subTest(name):
                Block.objects.all().delete()
                self._block(**ends)

                self.assertEqual(self._preview(self.KEY), UNAVAILABLE)

    def test_org_author_suspended_or_inactive(self):
        org_post = Post.objects.create(
            author_org=self.other_org,
            content="Club news",
            visibility=Post.Visibility.PUBLIC,
        )
        self._share("post", org_post.id)

        for fields in ({"is_suspended": True}, {"is_active": False}):
            with self.subTest(fields):
                Organization.objects.filter(id=self.other_org.id).update(
                    is_suspended=False, is_active=True
                )
                Organization.objects.filter(id=self.other_org.id).update(**fields)

                self.assertEqual(self._preview(self.KEY), UNAVAILABLE)

    def test_the_same_rule_refuses_a_new_share(self):
        self._block(blocker_user=self.author, blocked_user=self.sender)

        response = self.client.post(
            SHARE_URL,
            self._share_body(
                "post", self.post.id, conversation_ids=[str(self.conversation.id)]
            ),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class SharedRecruitmentCases(UnavailableBase):
    KEY = "shared_recruitment_preview"

    def setUp(self):
        super().setUp()
        self._share("recruitment", self.recruitment.id)

    def test_org_suspended_or_inactive(self):
        for fields in ({"is_suspended": True}, {"is_active": False}):
            with self.subTest(fields):
                Organization.objects.filter(id=self.other_org.id).update(
                    is_suspended=False, is_active=True
                )
                Organization.objects.filter(id=self.other_org.id).update(**fields)

                self.assertEqual(self._preview(self.KEY), UNAVAILABLE)

    def test_a_block_in_either_direction(self):
        for name, ends in (
            ("org blocked viewer", {"blocker_org": self.other_org, "blocked_user": self.sender}),
            ("viewer blocked org", {"blocker_user": self.sender, "blocked_org": self.other_org}),
        ):
            with self.subTest(name):
                Block.objects.all().delete()
                self._block(**ends)

                self.assertEqual(self._preview(self.KEY), UNAVAILABLE)

    def test_private_and_followers_only(self):
        for visibility in (
            Recruitment.Visibility.PRIVATE,
            Recruitment.Visibility.FOLLOWERS_ONLY,
        ):
            with self.subTest(visibility):
                Recruitment.objects.filter(id=self.recruitment.id).update(
                    visibility=visibility
                )

                self.assertEqual(self._preview(self.KEY), UNAVAILABLE)

    def test_hard_deleted(self):
        self.recruitment.delete()

        self.assertEqual(self._preview(self.KEY), UNAVAILABLE)


class SharedProfileCases(UnavailableBase):
    USER_KEY = "shared_user_profile_preview"
    ORG_KEY = "shared_org_profile_preview"

    def test_user_deactivated(self):
        self._share("user", self.author.id)
        self._deactivate(self.author)

        self.assertEqual(self._preview(self.USER_KEY), UNAVAILABLE)

    def test_user_deleted_their_account(self):
        self._share("user", self.author.id)
        self._delete_account(self.author)

        self.assertEqual(self._preview(self.USER_KEY), UNAVAILABLE)

    def test_user_hard_deleted(self):
        self._share("user", self.author.id)
        self.author.delete()

        self.assertEqual(self._preview(self.USER_KEY), UNAVAILABLE)

    def test_user_block_in_either_direction(self):
        self._share("user", self.author.id)

        for ends in (
            {"blocker_user": self.author, "blocked_user": self.sender},
            {"blocker_user": self.sender, "blocked_user": self.author},
        ):
            with self.subTest(ends):
                Block.objects.all().delete()
                self._block(**ends)

                self.assertEqual(self._preview(self.USER_KEY), UNAVAILABLE)

    def test_org_suspended_or_inactive(self):
        self._share("organization", self.other_org.id)

        for fields in ({"is_suspended": True}, {"is_active": False}):
            with self.subTest(fields):
                Organization.objects.filter(id=self.other_org.id).update(
                    is_suspended=False, is_active=True
                )
                Organization.objects.filter(id=self.other_org.id).update(**fields)

                self.assertEqual(self._preview(self.ORG_KEY), UNAVAILABLE)

    def test_org_block(self):
        self._share("organization", self.other_org.id)
        self._block(blocker_org=self.other_org, blocked_user=self.sender)

        self.assertEqual(self._preview(self.ORG_KEY), UNAVAILABLE)


class DeletedSenderTests(UnavailableBase):
    def setUp(self):
        super().setUp()
        Message.objects.create(
            conversation=self.conversation,
            sender_user=self.receiver,
            content="old message",
        )

    def _assert_placeholder(self):
        sender = self._rows()[0]["sender"]

        self.assertEqual(sender["name"], DELETED_ACCOUNT_NAME)
        self.assertEqual(sender["name"], "Deleted account")
        self.assertEqual(sender["avatar"], "")
        self.assertEqual(sender["username"], "")
        # How a client still tells its own bubbles from the other side's.
        self.assertEqual(sender["id"], str(self.receiver.id))

    def test_pending_the_purge(self):
        self._delete_account(self.receiver)

        self._assert_placeholder()

    def test_after_the_purge_anonymized_it(self):
        User.objects.filter(pk=self.receiver.pk).update(
            is_active=False,
            username=None,
            deletion_requested_at=timezone.now(),
        )

        self._assert_placeholder()

    def test_a_suspended_sender_keeps_their_name(self):
        self._deactivate(self.receiver)

        sender = self._rows()[0]["sender"]

        self.assertEqual(sender["username"], "receiver")

    def test_a_person_who_never_left_is_untouched(self):
        sender = self._rows()[0]["sender"]

        self.assertEqual(sender["name"], "Receiver")


class CounterpartTests(UnavailableBase):
    def _from_detail(self):
        response = self.client.get(f"/conversations/{self.conversation.id}/details")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def _from_list(self):
        Message.objects.create(
            conversation=self.conversation, sender_user=self.sender, content="hi"
        )
        Conversation.objects.filter(id=self.conversation.id).update(
            last_message=Message.objects.filter(conversation=self.conversation).first(),
            last_message_at=timezone.now(),
        )
        response = self.client.get("/conversations/list")
        self.assertEqual(response.status_code, 200, response.data)
        return next(
            row for row in response.data["data"]
            if row["id"] == str(self.conversation.id)
        )

    def _assert_flagged(self, payload, reason):
        self.assertIs(payload["counterpart_unavailable"], True)
        self.assertEqual(payload["counterpart_unavailable_reason"], reason)

    def test_fine_when_they_are_still_here(self):
        payload = self._from_detail()

        self.assertIs(payload["counterpart_unavailable"], False)
        self.assertEqual(payload["counterpart_unavailable_reason"], "")

    def test_deleted_on_list_and_detail(self):
        self._delete_account(self.receiver)

        self._assert_flagged(self._from_detail(), COUNTERPART_DELETED_MESSAGE)
        self._assert_flagged(self._from_list(), COUNTERPART_DELETED_MESSAGE)

    def test_a_deleted_counterpart_is_the_placeholder(self):
        self._delete_account(self.receiver)

        other = self._from_detail()["other_participant"]

        self.assertEqual(other["name"], "Deleted account")
        self.assertEqual(other["username"], "")

    def test_deactivated_on_list_and_detail(self):
        self._deactivate(self.receiver)

        self._assert_flagged(self._from_detail(), COUNTERPART_INACTIVE_MESSAGE)
        self._assert_flagged(self._from_list(), COUNTERPART_INACTIVE_MESSAGE)

    def test_an_org_suspended_or_inactive(self):
        self.conversation = self._conversation(
            actor_user=self.sender, target_org=self.other_org
        )

        for fields in ({"is_suspended": True}, {"is_active": False}):
            with self.subTest(fields):
                Organization.objects.filter(id=self.other_org.id).update(
                    is_suspended=False, is_active=True
                )
                Organization.objects.filter(id=self.other_org.id).update(**fields)

                self._assert_flagged(
                    self._from_detail(), COUNTERPART_ORG_INACTIVE_MESSAGE
                )

    def test_the_other_row_gone_altogether(self):
        ConversationParticipant.objects.filter(
            conversation=self.conversation, user=self.receiver
        ).delete()

        payload = self._from_detail()

        self._assert_flagged(payload, COUNTERPART_DELETED_MESSAGE)
        self.assertEqual(payload["other_participant"]["name"], "Deleted account")
        self.assertIs(payload["is_blocked"], False)

    def test_a_text_send_is_refused_with_the_sentence(self):
        self._deactivate(self.receiver)

        with self.assertRaises(CounterpartUnavailableError) as raised:
            MessageService.send_message(
                self.conversation, sender_user=self.sender, content="hello?"
            )

        self.assertEqual(str(raised.exception), COUNTERPART_INACTIVE_MESSAGE)
        self.assertFalse(Message.objects.filter(conversation=self.conversation).exists())

    def test_the_media_endpoint_answers_400_with_the_sentence(self):
        self._delete_account(self.receiver)

        # The upload checks are a different subject, and run first.
        with patch.object(MessageService, "_validate_chat_image"):
            response = self.client.post(
                f"/conversations/{self.conversation.id}/messages/media",
                {
                    "media_type": "image",
                    "media_url": "https://media.goatza.com/chat/users/x/a.jpg",
                    "media_public_id": "chat/users/x/a.jpg",
                },
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        self.assertEqual(response.data["message"], COUNTERPART_DELETED_MESSAGE)
        self.assertEqual(response.data["data"]["reason"], "counterpart_unavailable")

    def test_a_share_reports_it_against_that_one_target(self):
        self._deactivate(self.receiver)

        response = self.client.post(
            SHARE_URL,
            self._share_body(
                "post", self.post.id, conversation_ids=[str(self.conversation.id)]
            ),
            format="json",
        )

        self.assertEqual(
            response.data["data"]["failed"],
            [{"id": str(self.conversation.id), "reason": "counterpart_unavailable"}],
        )

    def test_a_new_share_to_a_deactivated_person_opens_no_thread(self):
        self._deactivate(self.author)
        before = Conversation.objects.count()

        response = self.client.post(
            SHARE_URL,
            self._share_body(
                "post",
                self.post.id,
                recipients=[{"actor_type": "user", "actor_id": str(self.receiver.id)}],
            ),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(Conversation.objects.count(), before)

    def test_the_thread_can_still_be_read(self):
        Message.objects.create(
            conversation=self.conversation, sender_user=self.receiver, content="old"
        )
        self._delete_account(self.receiver)

        self.assertEqual(self._rows()[0]["content"], "old")


class MediaTests(UnavailableBase):
    def _media_message(self, **fields):
        fields.setdefault("message_type", Message.Type.IMAGE)
        return Message.objects.create(
            conversation=self.conversation, sender_user=self.sender, **fields
        )

    def test_a_photo_with_a_file_is_fine(self):
        self._media_message(
            media_url="https://media.goatza.com/chat/a.jpg",
            media_public_id="chat/a.jpg",
        )

        row = self._rows()[0]

        self.assertIs(row["media_unavailable"], False)
        self.assertEqual(row["media_url"], "https://media.goatza.com/chat/a.jpg")

    def test_a_photo_or_video_with_no_file_is_unavailable_and_blank(self):
        for kind in (Message.Type.IMAGE, Message.Type.VIDEO):
            with self.subTest(kind):
                self._media_message(
                    message_type=kind,
                    media_url="",
                    media_thumbnail_url="https://media.goatza.com/chat/poster.jpg",
                    media_public_id="chat/a",
                )

                row = self._rows()[0]

                self.assertIs(row["media_unavailable"], True)
                self.assertEqual(row["media_url"], "")
                self.assertEqual(row["media_thumbnail_url"], "")
                self.assertEqual(row["media_public_id"], "")

    def test_a_taken_down_message_keeps_no_address(self):
        from apps.messaging.serializers.message_serializers import MessageSerializer

        message = self._media_message(
            media_url="https://media.goatza.com/chat/a.jpg",
            media_public_id="chat/a.jpg",
            is_deleted=True,
        )

        data = MessageSerializer(message, context={"viewer": None}).data

        self.assertIs(data["media_unavailable"], True)
        self.assertEqual(data["media_url"], "")

    def test_a_text_message_is_never_flagged(self):
        Message.objects.create(
            conversation=self.conversation, sender_user=self.sender, content="hi"
        )

        self.assertIs(self._rows()[0]["media_unavailable"], False)
