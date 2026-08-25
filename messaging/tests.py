import uuid
from unittest.mock import patch

from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from datetime import timedelta
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User, UserProfile
from connections.models import Follow
from messaging.models import Conversation, ConversationParticipant, Message
from messaging.services.conversation_service import ConversationService
from messaging.services.exceptions import ContentUnavailableError, InvalidMediaError
from messaging.services.message_service import MessageService
from notifications.models import Notification
from organization.models import Organization, OrganizationMember
from posts.models import Post, PostMedia
from recruitments.models import Recruitment, RecruitmentMedia
from sports.models import Sport

SHARE_URL = "/conversations/share"
MESSAGES_URL = "/conversations/messages/list"

# Redis is not available in tests and every send fans out over the channel
# layer. In-memory keeps the real code path (group_send is still exercised)
# without needing a broker.
CHANNEL_LAYERS = {
    "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}
}


@override_settings(CHANNEL_LAYERS=CHANNEL_LAYERS)
class MessagingShareTestCase(APITestCase):
    """Shared fixtures: three users, two orgs, a post and a recruitment."""

    def setUp(self):
        # Throttle state lives in the default cache and leaks between tests,
        # 429-ing whichever test happens to run later.
        cache.clear()

        self.sender = self._user("sender")
        self.receiver = self._user("receiver")
        self.author = self._user("author")

        self.org = Organization.objects.create(
            name="Dream FC", username="dreamfc", type=Organization.Type.CLUB,
        )
        OrganizationMember.objects.create(
            organization=self.org,
            user=self.sender,
            role=OrganizationMember.Role.OWNER,
        )

        self.other_org = Organization.objects.create(
            name="Rival FC", username="rivalfc", type=Organization.Type.CLUB,
        )

        self.sport = Sport.objects.create(name="Football", icon_name="mdi:soccer")

        self.post = Post.objects.create(
            author_user=self.author,
            content="Public highlight reel",
            visibility=Post.Visibility.PUBLIC,
        )
        PostMedia.objects.create(
            post=self.post,
            file_url="https://cdn.test/clip.mp4",
            public_id="posts/clip",
            media_type=PostMedia.MediaType.VIDEO,
            thumbnail_url="https://cdn.test/clip.jpg",
            order=0,
        )

        self.recruitment = Recruitment.objects.create(
            organization=self.other_org,
            sport=self.sport,
            title="U17 Open Trials",
            recruitment_type=Recruitment.Type.OPEN_TRIAL,
            status=Recruitment.Status.ACTIVE,
            visibility=Recruitment.Visibility.PUBLIC,
            application_deadline=timezone.now() + timedelta(days=7),
        )
        RecruitmentMedia.objects.create(
            recruitment=self.recruitment,
            file_url="https://cdn.test/trial.jpg",
            public_id="recruitments/trial",
            media_type=RecruitmentMedia.MediaType.IMAGE,
            order=0,
        )

        self.client.force_authenticate(user=self.sender)

    # ── helpers ──────────────────────────────────────────────────

    @staticmethod
    def _user(username):
        """
        A user WITH a profile — signup always creates one
        (accounts.views.user_auth_views), and the push payload reads
        sender_user.profile_name, which raises on a profile-less user.
        """
        user = User.objects.create_user(
            email=f"{username}@example.com",
            password="pass1234",
            username=username,
        )
        UserProfile.objects.create(user=user, name=username.title())
        return user

    def _org_headers(self, org=None):
        return {
            "HTTP_X_ACTOR_TYPE": "organization",
            "HTTP_X_ACTOR_ID": str((org or self.org).id),
        }

    def _conversation(self, actor_user=None, actor_org=None,
                      target_user=None, target_org=None):
        conversation, _ = ConversationService.get_or_create_conversation(
            actor_user=actor_user, actor_org=actor_org,
            target_user=target_user, target_org=target_org,
        )
        return conversation

    def _mutual_conversation(self):
        """An ACTIVE conversation between sender and receiver."""
        Follow.objects.get_or_create(
            follower_user=self.sender, following_user=self.receiver
        )
        Follow.objects.get_or_create(
            follower_user=self.receiver, following_user=self.sender
        )
        return self._conversation(actor_user=self.sender, target_user=self.receiver)

    def _share_body(self, target_type="post", target_id=None, **extra):
        body = {
            "target": {
                "type": target_type,
                "id": str(target_id or self.post.id),
            },
        }
        body.update(extra)
        return body


class SharePostTests(MessagingShareTestCase):

    def test_share_post_as_user_actor(self):
        conversation = self._mutual_conversation()

        response = self.client.post(
            SHARE_URL,
            self._share_body(
                conversation_ids=[str(conversation.id)],
                note="watch this",
            ),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"]["sent"], [str(conversation.id)])
        self.assertEqual(response.data["data"]["failed"], [])

        message = Message.objects.get(conversation=conversation)
        self.assertEqual(message.message_type, Message.Type.SHARED_POST)
        self.assertEqual(message.shared_post_id, self.post.id)
        self.assertEqual(message.sender_user_id, self.sender.id)
        self.assertIsNone(message.sender_org_id)
        # the note becomes the message body
        self.assertEqual(message.content, "watch this")

    def test_share_post_as_org_actor(self):
        conversation = self._conversation(
            actor_org=self.org, target_user=self.receiver
        )

        response = self.client.post(
            SHARE_URL,
            self._share_body(conversation_ids=[str(conversation.id)]),
            format="json",
            **self._org_headers(),
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"]["sent"], [str(conversation.id)])

        message = Message.objects.get(conversation=conversation)
        self.assertEqual(message.sender_org_id, self.org.id)
        self.assertIsNone(message.sender_user_id)
        self.assertEqual(message.shared_post_id, self.post.id)

    def test_share_recruitment(self):
        conversation = self._mutual_conversation()

        response = self.client.post(
            SHARE_URL,
            self._share_body(
                target_type="recruitment",
                target_id=self.recruitment.id,
                conversation_ids=[str(conversation.id)],
            ),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        message = Message.objects.get(conversation=conversation)
        self.assertEqual(message.message_type, Message.Type.SHARED_RECRUITMENT)
        self.assertEqual(message.shared_recruitment_id, self.recruitment.id)

    def test_share_recruitment_as_org_actor(self):
        conversation = self._conversation(
            actor_org=self.org, target_user=self.receiver
        )

        response = self.client.post(
            SHARE_URL,
            self._share_body(
                target_type="recruitment",
                target_id=self.recruitment.id,
                conversation_ids=[str(conversation.id)],
            ),
            format="json",
            **self._org_headers(),
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        message = Message.objects.get(conversation=conversation)
        self.assertEqual(message.sender_org_id, self.org.id)
        self.assertEqual(message.shared_recruitment_id, self.recruitment.id)

    def test_share_notifies_recipient(self):
        conversation = self._mutual_conversation()

        self.client.post(
            SHARE_URL,
            self._share_body(conversation_ids=[str(conversation.id)]),
            format="json",
        )

        notification = Notification.objects.get(recipient_user=self.receiver)
        self.assertEqual(notification.type, Notification.Type.MESSAGE)
        self.assertEqual(notification.actor_user_id, self.sender.id)
        self.assertEqual(
            notification.data["conversation_id"], str(conversation.id)
        )
        # grouped per conversation, deduped per message
        self.assertEqual(
            notification.group_key,
            f"message_share:conversation:{conversation.id}",
        )
        # the sender does not notify themselves
        self.assertFalse(
            Notification.objects.filter(recipient_user=self.sender).exists()
        )

    def test_share_notification_does_not_leak_the_shared_content(self):
        """
        The notification list renders notification.post with no visibility
        check, so a share notification must not carry the FK — the recipient
        may not be allowed to see what was shared.
        """
        post = Post.objects.create(
            author_user=self.author,
            content="Secret training footage",
            visibility=Post.Visibility.FOLLOWERS,
        )
        Follow.objects.create(
            follower_user=self.sender, following_user=self.author
        )
        conversation = self._mutual_conversation()

        MessageService.send_shared_post(
            conversation=conversation, sender_user=self.sender, post=post,
        )

        notification = Notification.objects.get(recipient_user=self.receiver)
        self.assertIsNone(notification.post_id)
        self.assertIsNone(notification.recruitment_id)

        self.client.force_authenticate(user=self.receiver)
        response = self.client.get("/notifications/list")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("Secret training footage", str(response.data))

    def test_repeated_share_of_same_message_is_deduped(self):
        conversation = self._mutual_conversation()
        message = MessageService.send_shared_post(
            conversation=conversation, sender_user=self.sender, post=self.post,
        )

        # re-dispatching the same message must not write a second row
        from notifications.services.notification_service import NotificationService
        NotificationService.message_share(message, recipient_user=self.receiver)

        self.assertEqual(
            Notification.objects.filter(recipient_user=self.receiver).count(), 1
        )

    def test_share_to_multiple_conversations_reports_partial_success(self):
        good = self._mutual_conversation()
        # sender is not a participant of this one
        stranger_conversation = self._conversation(
            actor_user=self.receiver, target_user=self.author
        )
        missing_id = "00000000-0000-0000-0000-000000000000"

        response = self.client.post(
            SHARE_URL,
            self._share_body(
                conversation_ids=[
                    str(good.id), str(stranger_conversation.id), missing_id,
                ],
            ),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertEqual(data["sent"], [str(good.id)])
        self.assertEqual(
            sorted(data["failed"], key=lambda f: f["reason"]),
            [
                {"id": missing_id, "reason": "conversation_not_found"},
                {
                    "id": str(stranger_conversation.id),
                    "reason": "not_a_participant",
                },
            ],
        )
        # the failures wrote nothing
        self.assertEqual(Message.objects.count(), 1)


class ShareToStrangerTests(MessagingShareTestCase):

    def test_share_to_stranger_creates_request_conversation(self):
        response = self.client.post(
            SHARE_URL,
            self._share_body(
                recipients=[
                    {"actor_type": "user", "actor_id": str(self.receiver.id)}
                ],
            ),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]["sent"]), 1)

        conversation = Conversation.objects.get(
            id=response.data["data"]["sent"][0]
        )
        # no mutual follow → lands as a message request
        self.assertEqual(conversation.status, Conversation.Status.REQUESTED)

        sender_participant = ConversationParticipant.objects.get(
            conversation=conversation, user=self.sender
        )
        receiver_participant = ConversationParticipant.objects.get(
            conversation=conversation, user=self.receiver
        )
        self.assertTrue(sender_participant.has_accepted)
        self.assertFalse(receiver_participant.has_accepted)

        message = Message.objects.get(conversation=conversation)
        self.assertEqual(message.message_type, Message.Type.SHARED_POST)

    def test_share_to_mutual_follower_creates_active_conversation(self):
        Follow.objects.create(
            follower_user=self.sender, following_user=self.receiver
        )
        Follow.objects.create(
            follower_user=self.receiver, following_user=self.sender
        )

        response = self.client.post(
            SHARE_URL,
            self._share_body(
                recipients=[
                    {"actor_type": "user", "actor_id": str(self.receiver.id)}
                ],
            ),
            format="json",
        )

        conversation = Conversation.objects.get(
            id=response.data["data"]["sent"][0]
        )
        self.assertEqual(conversation.status, Conversation.Status.ACTIVE)

    def test_share_to_org_recipient(self):
        response = self.client.post(
            SHARE_URL,
            self._share_body(
                recipients=[
                    {
                        "actor_type": "organization",
                        "actor_id": str(self.other_org.id),
                    }
                ],
            ),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        conversation = Conversation.objects.get(
            id=response.data["data"]["sent"][0]
        )
        self.assertTrue(
            ConversationParticipant.objects.filter(
                conversation=conversation, org=self.other_org
            ).exists()
        )

    def test_recipient_reuses_existing_conversation(self):
        existing = self._mutual_conversation()

        response = self.client.post(
            SHARE_URL,
            self._share_body(
                recipients=[
                    {"actor_type": "user", "actor_id": str(self.receiver.id)}
                ],
            ),
            format="json",
        )

        self.assertEqual(response.data["data"]["sent"], [str(existing.id)])
        self.assertEqual(Conversation.objects.count(), 1)

    def test_recipient_and_conversation_id_for_same_thread_sends_once(self):
        existing = self._mutual_conversation()

        response = self.client.post(
            SHARE_URL,
            self._share_body(
                conversation_ids=[str(existing.id)],
                recipients=[
                    {"actor_type": "user", "actor_id": str(self.receiver.id)}
                ],
            ),
            format="json",
        )

        self.assertEqual(response.data["data"]["sent"], [str(existing.id)])
        self.assertEqual(Message.objects.count(), 1)

    def test_share_to_unknown_recipient_fails_that_target_only(self):
        good = self._mutual_conversation()
        missing_id = "00000000-0000-0000-0000-000000000000"

        response = self.client.post(
            SHARE_URL,
            self._share_body(
                conversation_ids=[str(good.id)],
                recipients=[{"actor_type": "user", "actor_id": missing_id}],
            ),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"]["sent"], [str(good.id)])
        self.assertEqual(
            response.data["data"]["failed"],
            [{"id": missing_id, "reason": "recipient_not_found"}],
        )


class ShareVisibilityTests(MessagingShareTestCase):

    def _followers_only_post(self):
        return Post.objects.create(
            author_user=self.author,
            content="Followers only",
            visibility=Post.Visibility.FOLLOWERS,
        )

    def test_cannot_share_followers_only_post_you_cannot_see(self):
        post = self._followers_only_post()
        conversation = self._mutual_conversation()

        response = self.client.post(
            SHARE_URL,
            self._share_body(
                target_id=post.id, conversation_ids=[str(conversation.id)]
            ),
            format="json",
        )

        # 404, not 403 — a distinct status would confirm the post exists
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(response.data["success"])
        self.assertEqual(Message.objects.count(), 0)

    def test_can_share_followers_only_post_you_follow(self):
        post = self._followers_only_post()
        Follow.objects.create(
            follower_user=self.sender, following_user=self.author
        )
        conversation = self._mutual_conversation()

        response = self.client.post(
            SHARE_URL,
            self._share_body(
                target_id=post.id, conversation_ids=[str(conversation.id)]
            ),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"]["sent"], [str(conversation.id)])

    def test_can_share_own_followers_only_post(self):
        post = Post.objects.create(
            author_user=self.sender,
            content="My own private post",
            visibility=Post.Visibility.FOLLOWERS,
        )
        conversation = self._mutual_conversation()

        response = self.client.post(
            SHARE_URL,
            self._share_body(
                target_id=post.id, conversation_ids=[str(conversation.id)]
            ),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_cannot_share_deleted_post(self):
        self.post.is_deleted = True
        self.post.save(update_fields=["is_deleted"])
        conversation = self._mutual_conversation()

        response = self.client.post(
            SHARE_URL,
            self._share_body(conversation_ids=[str(conversation.id)]),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_cannot_share_draft_recruitment(self):
        self.recruitment.status = Recruitment.Status.DRAFT
        self.recruitment.save(update_fields=["status"])
        conversation = self._mutual_conversation()

        response = self.client.post(
            SHARE_URL,
            self._share_body(
                target_type="recruitment",
                target_id=self.recruitment.id,
                conversation_ids=[str(conversation.id)],
            ),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_can_share_closed_recruitment(self):
        self.recruitment.status = Recruitment.Status.CLOSED
        self.recruitment.save(update_fields=["status"])
        conversation = self._mutual_conversation()

        response = self.client.post(
            SHARE_URL,
            self._share_body(
                target_type="recruitment",
                target_id=self.recruitment.id,
                conversation_ids=[str(conversation.id)],
            ),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_cannot_share_followers_only_recruitment_you_do_not_follow(self):
        self.recruitment.visibility = Recruitment.Visibility.FOLLOWERS_ONLY
        self.recruitment.save(update_fields=["visibility"])
        conversation = self._mutual_conversation()

        response = self.client.post(
            SHARE_URL,
            self._share_body(
                target_type="recruitment",
                target_id=self.recruitment.id,
                conversation_ids=[str(conversation.id)],
            ),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_service_rejects_share_of_invisible_post(self):
        post = self._followers_only_post()
        conversation = self._mutual_conversation()

        with self.assertRaises(ContentUnavailableError):
            MessageService.send_shared_post(
                conversation=conversation,
                sender_user=self.sender,
                post=post,
            )

        self.assertEqual(Message.objects.count(), 0)


class SharePreviewTests(MessagingShareTestCase):

    def _share_and_list(self, as_user=None):
        conversation = self._mutual_conversation()
        MessageService.send_shared_post(
            conversation=conversation,
            sender_user=self.sender,
            post=self.post,
            note="look",
        )

        self.client.force_authenticate(user=as_user or self.receiver)
        return conversation, self.client.get(
            MESSAGES_URL, {"conversation_id": str(conversation.id)}
        )

    def test_preview_renders_for_viewer_who_can_see_post(self):
        _, response = self._share_and_list()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        message = response.data["data"]["results"][0]

        self.assertEqual(message["message_type"], "shared_post")
        self.assertEqual(message["content"], "look")
        self.assertIsNone(message["shared_recruitment_preview"])

        preview = message["shared_post_preview"]
        self.assertFalse(preview["unavailable"])
        self.assertEqual(preview["id"], str(self.post.id))
        self.assertEqual(preview["text"], "Public highlight reel")
        self.assertEqual(preview["author"]["username"], "author")
        self.assertEqual(preview["media_count"], 1)
        self.assertEqual(preview["thumbnail_url"], "https://cdn.test/clip.jpg")

    def test_text_snippet_is_truncated_to_120_chars(self):
        self.post.content = "x" * 200
        self.post.save(update_fields=["content"])

        _, response = self._share_and_list()
        preview = response.data["data"]["results"][0]["shared_post_preview"]

        self.assertEqual(len(preview["text"]), 120)
        self.assertTrue(preview["is_text_truncated"])

    def test_deleted_shared_post_renders_unavailable_and_does_not_crash(self):
        conversation = self._mutual_conversation()
        MessageService.send_shared_post(
            conversation=conversation, sender_user=self.sender, post=self.post,
        )

        # hard delete → the FK is SET_NULL, the message survives
        post_id = self.post.id
        self.post.delete()

        message = Message.objects.get(conversation=conversation)
        self.assertIsNone(message.shared_post_id)
        self.assertEqual(message.message_type, Message.Type.SHARED_POST)

        self.client.force_authenticate(user=self.receiver)
        response = self.client.get(
            MESSAGES_URL, {"conversation_id": str(conversation.id)}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        preview = response.data["data"]["results"][0]["shared_post_preview"]
        self.assertEqual(preview, {"unavailable": True})
        self.assertNotIn(str(post_id), str(response.data))

    def test_soft_deleted_shared_post_renders_unavailable(self):
        conversation = self._mutual_conversation()
        MessageService.send_shared_post(
            conversation=conversation, sender_user=self.sender, post=self.post,
        )
        self.post.is_deleted = True
        self.post.save(update_fields=["is_deleted"])

        self.client.force_authenticate(user=self.receiver)
        response = self.client.get(
            MESSAGES_URL, {"conversation_id": str(conversation.id)}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["data"]["results"][0]["shared_post_preview"],
            {"unavailable": True},
        )

    def test_preview_hidden_from_viewer_who_cannot_see_the_post(self):
        """
        The sender follows the author and may share; the RECEIVER does not, so
        the preview must not leak the post's content to them.
        """
        post = Post.objects.create(
            author_user=self.author,
            content="Secret training footage",
            visibility=Post.Visibility.FOLLOWERS,
        )
        Follow.objects.create(
            follower_user=self.sender, following_user=self.author
        )
        conversation = self._mutual_conversation()
        MessageService.send_shared_post(
            conversation=conversation, sender_user=self.sender, post=post,
        )

        self.client.force_authenticate(user=self.receiver)
        response = self.client.get(
            MESSAGES_URL, {"conversation_id": str(conversation.id)}
        )

        preview = response.data["data"]["results"][0]["shared_post_preview"]
        self.assertEqual(preview, {"unavailable": True})
        self.assertNotIn("Secret training footage", str(response.data))

        # …but the sender, who can see it, still gets the full preview
        self.client.force_authenticate(user=self.sender)
        response = self.client.get(
            MESSAGES_URL, {"conversation_id": str(conversation.id)}
        )
        self.assertFalse(
            response.data["data"]["results"][0]
            ["shared_post_preview"]["unavailable"]
        )

    def test_recruitment_preview_fields(self):
        conversation = self._mutual_conversation()
        MessageService.send_shared_recruitment(
            conversation=conversation,
            sender_user=self.sender,
            recruitment=self.recruitment,
        )

        self.client.force_authenticate(user=self.receiver)
        response = self.client.get(
            MESSAGES_URL, {"conversation_id": str(conversation.id)}
        )

        message = response.data["data"]["results"][0]
        self.assertIsNone(message["shared_post_preview"])

        preview = message["shared_recruitment_preview"]
        self.assertFalse(preview["unavailable"])
        self.assertEqual(preview["title"], "U17 Open Trials")
        self.assertEqual(preview["org"]["username"], "rivalfc")
        self.assertEqual(preview["sport"], "Football")
        self.assertEqual(preview["type"], "open_trial")
        self.assertEqual(preview["status"], "active")
        self.assertEqual(preview["cover_url"], "https://cdn.test/trial.jpg")
        self.assertIsNotNone(preview["deadline"])

    def test_deleted_shared_recruitment_renders_unavailable(self):
        conversation = self._mutual_conversation()
        MessageService.send_shared_recruitment(
            conversation=conversation,
            sender_user=self.sender,
            recruitment=self.recruitment,
        )
        self.recruitment.is_deleted = True
        self.recruitment.save(update_fields=["is_deleted"])

        self.client.force_authenticate(user=self.receiver)
        response = self.client.get(
            MESSAGES_URL, {"conversation_id": str(conversation.id)}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["data"]["results"][0]["shared_recruitment_preview"],
            {"unavailable": True},
        )

    def test_text_message_has_null_previews_and_empty_media_fields(self):
        conversation = self._mutual_conversation()
        MessageService.send_message(
            conversation=conversation, sender_user=self.sender, content="hi",
        )

        response = self.client.get(
            MESSAGES_URL, {"conversation_id": str(conversation.id)}
        )

        message = response.data["data"]["results"][0]
        self.assertIsNone(message["shared_post_preview"])
        self.assertIsNone(message["shared_recruitment_preview"])
        self.assertIsNone(message["media_width"])
        self.assertEqual(message["media_public_id"], "")


class ConversationStateTests(MessagingShareTestCase):

    def test_share_updates_last_message_and_unread_count(self):
        conversation = self._mutual_conversation()

        response = self.client.post(
            SHARE_URL,
            self._share_body(conversation_ids=[str(conversation.id)]),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        message = Message.objects.get(conversation=conversation)
        conversation.refresh_from_db()
        self.assertEqual(conversation.last_message_id, message.id)
        self.assertIsNotNone(conversation.last_message_at)

        # the receiver has one unread; the sender has none
        self.client.force_authenticate(user=self.receiver)
        summary = self.client.get("/conversations/unread/summary")
        self.assertEqual(summary.data["data"]["chats"], 1)
        self.assertEqual(summary.data["data"]["total"], 1)

        self.client.force_authenticate(user=self.sender)
        summary = self.client.get("/conversations/unread/summary")
        self.assertEqual(summary.data["data"]["total"], 0)

    def test_second_share_moves_last_message_forward(self):
        conversation = self._mutual_conversation()

        first = MessageService.send_shared_post(
            conversation=conversation, sender_user=self.sender, post=self.post,
        )
        conversation.refresh_from_db()
        first_at = conversation.last_message_at
        self.assertEqual(conversation.last_message_id, first.id)

        second = MessageService.send_shared_recruitment(
            conversation=conversation,
            sender_user=self.sender,
            recruitment=self.recruitment,
        )
        conversation.refresh_from_db()

        self.assertEqual(conversation.last_message_id, second.id)
        self.assertGreaterEqual(conversation.last_message_at, first_at)

    def test_share_to_stranger_counts_as_a_request_not_a_chat(self):
        self.client.post(
            SHARE_URL,
            self._share_body(
                recipients=[
                    {"actor_type": "user", "actor_id": str(self.receiver.id)}
                ],
            ),
            format="json",
        )

        self.client.force_authenticate(user=self.receiver)
        summary = self.client.get("/conversations/unread/summary")

        self.assertEqual(summary.data["data"]["requests"], 1)
        self.assertEqual(summary.data["data"]["chats"], 0)

    def test_conversation_list_renders_shared_last_message(self):
        conversation = self._mutual_conversation()
        self.client.post(
            SHARE_URL,
            self._share_body(conversation_ids=[str(conversation.id)]),
            format="json",
        )

        self.client.force_authenticate(user=self.receiver)
        response = self.client.get("/conversations/list")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        last_message = response.data["data"][0]["last_message"]
        self.assertEqual(last_message["message_type"], "shared_post")
        self.assertFalse(last_message["shared_post_preview"]["unavailable"])

    def test_marking_read_clears_unread(self):
        conversation = self._mutual_conversation()
        self.client.post(
            SHARE_URL,
            self._share_body(conversation_ids=[str(conversation.id)]),
            format="json",
        )

        self.client.force_authenticate(user=self.receiver)
        self.client.post(
            "/conversations/mark/read/all",
            {"conversation_id": str(conversation.id)},
            format="json",
        )

        summary = self.client.get("/conversations/unread/summary")
        self.assertEqual(summary.data["data"]["total"], 0)


class ReadReceiptTests(MessagingShareTestCase):
    """
    Read state is derived from the recipient participant's ``last_read_at``
    rather than stored per message. These pin down the direction of that
    comparison — the easy thing to get backwards is answering "have I read
    this?" when the sender is asking "have THEY read this?".
    """

    def _drain(self, conversation):
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        layer = get_channel_layer()
        channel = async_to_sync(layer.new_channel)()
        async_to_sync(layer.group_add)(f"chat_{conversation.id}", channel)
        return layer, channel

    def _messages_for(self, user, conversation):
        self.client.force_authenticate(user=user)
        response = self.client.get(
            MESSAGES_URL, {"conversation_id": str(conversation.id)}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data["data"]["results"]

    def _mark_read(self, user, conversation):
        self.client.force_authenticate(user=user)
        response = self.client.post(
            "/conversations/mark/read/all",
            {"conversation_id": str(conversation.id)},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_message_is_unread_until_the_other_side_reads_it(self):
        conversation = self._mutual_conversation()
        MessageService.send_message(
            conversation=conversation, sender_user=self.sender, content="hi",
        )

        self.assertFalse(
            self._messages_for(self.sender, conversation)[0]["is_read"]
        )

        self._mark_read(self.receiver, conversation)

        self.assertTrue(
            self._messages_for(self.sender, conversation)[0]["is_read"]
        )

    def test_reading_a_thread_does_not_mark_your_own_messages_read(self):
        conversation = self._mutual_conversation()
        MessageService.send_message(
            conversation=conversation, sender_user=self.sender, content="hi",
        )

        # The sender opening their own chat must not turn their own ticks blue.
        self._mark_read(self.sender, conversation)

        self.assertFalse(
            self._messages_for(self.sender, conversation)[0]["is_read"]
        )

    def test_a_message_sent_after_the_read_is_unread_again(self):
        conversation = self._mutual_conversation()
        MessageService.send_message(
            conversation=conversation, sender_user=self.sender, content="first",
        )
        self._mark_read(self.receiver, conversation)
        MessageService.send_message(
            conversation=conversation, sender_user=self.sender, content="second",
        )

        read_by_content = {
            m["content"]: m["is_read"]
            for m in self._messages_for(self.sender, conversation)
        }

        self.assertTrue(read_by_content["first"])
        self.assertFalse(read_by_content["second"])

    def test_detail_exposes_the_other_sides_read_watermark(self):
        conversation = self._mutual_conversation()
        MessageService.send_message(
            conversation=conversation, sender_user=self.sender, content="hi",
        )
        detail_url = f"/conversations/{conversation.id}/details"

        self.client.force_authenticate(user=self.sender)
        response = self.client.get(detail_url)
        self.assertIsNone(response.data["data"]["other_last_read_at"])

        self._mark_read(self.receiver, conversation)

        self.client.force_authenticate(user=self.sender)
        response = self.client.get(detail_url)
        self.assertIsNotNone(response.data["data"]["other_last_read_at"])

    def test_websocket_echo_is_never_pre_marked_read(self):
        """
        The fan-out renders with no viewer, so is_read is False there — nobody
        can have read a message that is still being broadcast. The receipt
        arrives later, as its own event.
        """
        conversation = self._mutual_conversation()
        self._mark_read(self.receiver, conversation)

        layer, channel = self._drain(conversation)
        MessageService.send_message(
            conversation=conversation, sender_user=self.sender, content="hi",
        )

        from asgiref.sync import async_to_sync
        event = async_to_sync(layer.receive)(channel)

        self.assertFalse(event["message"]["is_read"])

    def test_marking_read_broadcasts_a_receipt_to_the_chat_group(self):
        conversation = self._mutual_conversation()
        MessageService.send_message(
            conversation=conversation, sender_user=self.sender, content="hi",
        )

        layer, channel = self._drain(conversation)
        self._mark_read(self.receiver, conversation)

        from asgiref.sync import async_to_sync
        event = async_to_sync(layer.receive)(channel)

        self.assertEqual(event["type"], "conversation_read")
        self.assertEqual(event["reader_id"], str(self.receiver.id))
        # A primitive — the channel layer cannot carry a datetime.
        self.assertIsInstance(event["last_read_at"], str)


class ShareValidationTests(MessagingShareTestCase):

    def test_requires_authentication(self):
        self.client.force_authenticate(user=None)

        response = self.client.post(
            SHARE_URL, self._share_body(conversation_ids=[]), format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_org_actor_requires_membership(self):
        response = self.client.post(
            SHARE_URL,
            self._share_body(conversation_ids=[]),
            format="json",
            **self._org_headers(org=self.other_org),   # sender is not a member
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_no_targets_is_a_structured_400(self):
        response = self.client.post(
            SHARE_URL, self._share_body(), format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("non_field_errors", response.data)

    def test_invalid_target_type_is_a_structured_400(self):
        response = self.client.post(
            SHARE_URL,
            self._share_body(target_type="banana", conversation_ids=[]),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("target", response.data)

    def test_more_than_ten_conversations_is_rejected(self):
        conversation = self._mutual_conversation()

        response = self.client.post(
            SHARE_URL,
            self._share_body(conversation_ids=[str(conversation.id)] * 11),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("conversation_ids", response.data)

    def test_cannot_share_with_yourself(self):
        response = self.client.post(
            SHARE_URL,
            self._share_body(
                recipients=[
                    {"actor_type": "user", "actor_id": str(self.sender.id)}
                ],
            ),
            format="json",
        )

        self.assertEqual(
            response.data["data"]["failed"],
            [{"id": str(self.sender.id), "reason": "cannot_share_with_self"}],
        )


class ShareThrottleTests(MessagingShareTestCase):

    def test_throttle_kicks_in_at_thirty_per_minute(self):
        conversation = self._mutual_conversation()
        body = self._share_body(conversation_ids=[str(conversation.id)])

        for i in range(30):
            response = self.client.post(SHARE_URL, body, format="json")
            self.assertEqual(
                response.status_code, status.HTTP_200_OK,
                f"share #{i + 1} should be allowed",
            )

        response = self.client.post(SHARE_URL, body, format="json")

        self.assertEqual(
            response.status_code, status.HTTP_429_TOO_MANY_REQUESTS
        )
        self.assertEqual(Message.objects.count(), 30)

    def test_org_actor_has_its_own_bucket(self):
        """
        The throttle keys on the actor, not the user — one person burning their
        personal quota must not lock their organization out.
        """
        user_conversation = self._mutual_conversation()
        org_conversation = self._conversation(
            actor_org=self.org, target_user=self.receiver
        )

        for _ in range(30):
            self.client.post(
                SHARE_URL,
                self._share_body(conversation_ids=[str(user_conversation.id)]),
                format="json",
            )

        # personal bucket is spent …
        response = self.client.post(
            SHARE_URL,
            self._share_body(conversation_ids=[str(user_conversation.id)]),
            format="json",
        )
        self.assertEqual(
            response.status_code, status.HTTP_429_TOO_MANY_REQUESTS
        )

        # … the org's is not
        response = self.client.post(
            SHARE_URL,
            self._share_body(conversation_ids=[str(org_conversation.id)]),
            format="json",
            **self._org_headers(),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class RealtimePayloadTests(MessagingShareTestCase):
    """
    The group_send contract. The consumer forwards this to the browser, so its
    shape is the websocket API.
    """

    def _drain(self, conversation):
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        layer = get_channel_layer()
        channel = async_to_sync(layer.new_channel)()
        async_to_sync(layer.group_add)(f"chat_{conversation.id}", channel)
        return layer, channel

    def test_text_message_carries_full_serialized_message(self):
        conversation = self._mutual_conversation()
        layer, channel = self._drain(conversation)

        MessageService.send_message(
            conversation=conversation, sender_user=self.sender, content="hello",
        )

        from asgiref.sync import async_to_sync
        event = async_to_sync(layer.receive)(channel)

        self.assertEqual(event["type"], "chat_message")

        # the new shape — identical to what the REST list returns
        message = event["message"]
        self.assertEqual(message["content"], "hello")
        self.assertEqual(message["message_type"], "text")
        self.assertEqual(message["sender"]["username"], "sender")
        self.assertIn("shared_post_preview", message)
        self.assertIn("media_width", message)

        # the deprecated flat keys the current web client still reads
        self.assertEqual(event["message_id"], message["id"])
        self.assertEqual(event["content"], "hello")
        self.assertEqual(event["sender"], message["sender"])

    def test_shared_post_payload_carries_preview(self):
        conversation = self._mutual_conversation()
        layer, channel = self._drain(conversation)

        MessageService.send_shared_post(
            conversation=conversation, sender_user=self.sender, post=self.post,
        )

        from asgiref.sync import async_to_sync
        event = async_to_sync(layer.receive)(channel)

        preview = event["message"]["shared_post_preview"]
        self.assertFalse(preview["unavailable"])
        self.assertEqual(preview["id"], str(self.post.id))

    def test_followers_only_share_is_not_broadcast_to_the_group(self):
        """
        One group_send serves every socket, so it is rendered with no viewer.
        Followers-only content must come back unavailable rather than be
        broadcast to participants who cannot see it — ChatConsumer re-renders
        it per socket for those who can.
        """
        post = Post.objects.create(
            author_user=self.author,
            content="Secret training footage",
            visibility=Post.Visibility.FOLLOWERS,
        )
        Follow.objects.create(
            follower_user=self.sender, following_user=self.author
        )
        conversation = self._mutual_conversation()
        layer, channel = self._drain(conversation)

        MessageService.send_shared_post(
            conversation=conversation, sender_user=self.sender, post=post,
        )

        from asgiref.sync import async_to_sync
        event = async_to_sync(layer.receive)(channel)

        self.assertEqual(
            event["message"]["shared_post_preview"], {"unavailable": True}
        )
        self.assertNotIn("Secret training footage", str(event))

    def test_payload_is_channel_layer_serializable(self):
        """
        The Redis layer msgpacks the event, which only carries primitives — a
        stray datetime/UUID in a hand-built preview dict would break every send
        in production while passing under the in-memory layer used here.
        """
        import msgpack

        conversation = self._mutual_conversation()
        layer, channel = self._drain(conversation)

        MessageService.send_shared_recruitment(
            conversation=conversation,
            sender_user=self.sender,
            recruitment=self.recruitment,
        )

        from asgiref.sync import async_to_sync
        event = async_to_sync(layer.receive)(channel)

        packed = msgpack.packb(event)
        self.assertEqual(
            msgpack.unpackb(packed)["message"]["id"], event["message"]["id"]
        )


class MessageListQueryCountTests(MessagingShareTestCase):

    # A page holding both share kinds costs:
    #   1. the participant/permission check
    #   2. the other participant's last_read_at (the read-receipt watermark
    #      behind every message's is_read — fetched once for the whole page)
    #   3. the message page itself (every preview field is select_related onto
    #      this one query)
    #   4. the shared_post media prefetch
    #   5. the shared_recruitment media prefetch
    # …and nothing per message. The viewer's follow graph is NOT loaded here:
    # public content answers "can you see it?" without consulting it.
    EXPECTED_QUERIES = 5

    def _seed(self, pairs, conversation):
        """`pairs` shared-recruitment + shared-post messages, own post each."""
        for i in range(pairs):
            post = Post.objects.create(
                author_user=self.author,
                content=f"post {i}",
                visibility=Post.Visibility.PUBLIC,
            )
            PostMedia.objects.create(
                post=post,
                file_url=f"https://cdn.test/{i}.jpg",
                public_id=f"posts/{i}",
                media_type=PostMedia.MediaType.IMAGE,
                order=0,
            )
            MessageService.send_shared_recruitment(
                conversation=conversation,
                sender_user=self.sender,
                recruitment=self.recruitment,
            )
            MessageService.send_shared_post(
                conversation=conversation, sender_user=self.sender, post=post,
            )

    def _list_with_pairs(self, pairs):
        conversation = self._mutual_conversation()
        self._seed(pairs, conversation)

        self.client.force_authenticate(user=self.receiver)

        with self.assertNumQueries(self.EXPECTED_QUERIES):
            response = self.client.get(
                MESSAGES_URL, {"conversation_id": str(conversation.id)}
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]["results"]), pairs * 2)

    def test_one_message_pair(self):
        self._list_with_pairs(1)

    def test_eight_message_pairs_cost_the_same(self):
        """
        The N+1 guard: 16 shared messages must cost exactly what 2 cost. Both
        tests assert the same EXPECTED_QUERIES constant, so a regression that
        made the count scale with the page would fail here while the 1-pair
        case still passed.
        """
        self._list_with_pairs(8)

    def test_followers_only_share_costs_one_extra_follow_query(self):
        """
        A hidden share makes the serializer consult the viewer's follow graph —
        once for the whole page, not once per message.
        """
        Follow.objects.create(
            follower_user=self.sender, following_user=self.author
        )
        conversation = self._mutual_conversation()

        for i in range(6):
            post = Post.objects.create(
                author_user=self.author,
                content=f"private {i}",
                visibility=Post.Visibility.FOLLOWERS,
            )
            MessageService.send_shared_post(
                conversation=conversation, sender_user=self.sender, post=post,
            )

        self.client.force_authenticate(user=self.receiver)

        # Same 5: permission check, read watermark, page, shared_post media —
        # and this time the follow graph (loaded once), but no
        # recruitment-media prefetch, since no message on this page shares one.
        with self.assertNumQueries(5):
            response = self.client.get(
                MESSAGES_URL, {"conversation_id": str(conversation.id)}
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(all(
            m["shared_post_preview"] == {"unavailable": True}
            for m in response.data["data"]["results"]
        ))


CLOUD = "democloud"


def _media_url(public_id, ext="jpg"):
    return f"https://media.goatza.test/{public_id}.{ext}"


class PhotoMessageTests(MessagingShareTestCase):
    """POST /conversations/<id>/messages/media — chat photos."""

    def _media_endpoint(self, conversation):
        return f"/conversations/{conversation.id}/messages/media"

    def _payload(self, sender_user=None, sender_org=None, ext="jpg", **over):
        if sender_org is not None:
            public_id = f"chat/organizations/{sender_org.id}/{uuid.uuid4()}"
        else:
            owner = sender_user or self.sender
            public_id = f"chat/users/{owner.id}/{uuid.uuid4()}"

        body = {
            "media_url": _media_url(public_id, ext),
            "media_public_id": public_id,
            "width": 1080,
            "height": 1350,
            "size_bytes": 512_000,
            "caption": "",
        }
        body.update(over)
        return body

    # ── happy paths ──────────────────────────────────────────────

    def test_send_photo_as_user_actor(self):
        conversation = self._mutual_conversation()

        response = self.client.post(
            self._media_endpoint(conversation),
            self._payload(sender_user=self.sender, caption="nice pitch"),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        message = Message.objects.get(conversation=conversation)
        self.assertEqual(message.message_type, Message.Type.IMAGE)
        self.assertEqual(message.sender_user_id, self.sender.id)
        self.assertEqual(message.content, "nice pitch")
        self.assertEqual(message.media_width, 1080)
        self.assertEqual(message.media_height, 1350)
        self.assertEqual(message.media_size_bytes, 512_000)
        self.assertTrue(
            message.media_public_id.startswith(f"chat/users/{self.sender.id}/")
        )

        data = response.data["data"]
        self.assertEqual(data["message_type"], "image")
        self.assertEqual(data["media_width"], 1080)
        self.assertIsNone(data["shared_post_preview"])

    def test_send_photo_as_org_actor(self):
        conversation = self._conversation(
            actor_org=self.org, target_user=self.receiver
        )

        response = self.client.post(
            self._media_endpoint(conversation),
            self._payload(sender_org=self.org),
            format="json",
            **self._org_headers(),
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        message = Message.objects.get(conversation=conversation)
        self.assertEqual(message.sender_org_id, self.org.id)
        self.assertIsNone(message.sender_user_id)
        self.assertEqual(message.message_type, Message.Type.IMAGE)

    def test_photo_updates_last_message_and_unread(self):
        conversation = self._mutual_conversation()

        self.client.post(
            self._media_endpoint(conversation),
            self._payload(sender_user=self.sender),
            format="json",
        )

        message = Message.objects.get(conversation=conversation)
        conversation.refresh_from_db()
        self.assertEqual(conversation.last_message_id, message.id)
        self.assertIsNotNone(conversation.last_message_at)

        self.client.force_authenticate(user=self.receiver)
        summary = self.client.get("/conversations/unread/summary")
        self.assertEqual(summary.data["data"]["chats"], 1)

    def test_photo_renders_in_message_list(self):
        conversation = self._mutual_conversation()
        self.client.post(
            self._media_endpoint(conversation),
            self._payload(sender_user=self.sender, caption="hi"),
            format="json",
        )

        self.client.force_authenticate(user=self.receiver)
        response = self.client.get(
            MESSAGES_URL, {"conversation_id": str(conversation.id)}
        )

        message = response.data["data"]["results"][0]
        self.assertEqual(message["message_type"], "image")
        self.assertEqual(message["content"], "hi")
        self.assertEqual(message["media_height"], 1350)

    # ── rejections ───────────────────────────────────────────────

    def test_non_participant_cannot_send_photo(self):
        conversation = self._conversation(
            actor_user=self.receiver, target_user=self.author
        )

        response = self.client.post(
            self._media_endpoint(conversation),
            self._payload(sender_user=self.sender),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data["data"]["reason"], "not_a_participant")
        self.assertEqual(Message.objects.count(), 0)

    def test_rejects_url_from_a_foreign_cloud(self):
        conversation = self._mutual_conversation()
        public_id = f"chat/users/{self.sender.id}/{uuid.uuid4()}"

        response = self.client.post(
            self._media_endpoint(conversation),
            {
                "media_url": (
                    f"https://media.goatza.test/"
                    f"v1/{public_id}.jpg"
                ),
                "media_public_id": public_id,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["data"]["reason"], "invalid_media")
        self.assertEqual(Message.objects.count(), 0)

    def test_rejects_public_id_outside_chat_folder(self):
        conversation = self._mutual_conversation()
        public_id = f"users/{self.sender.id}/posts/{uuid.uuid4()}"

        response = self.client.post(
            self._media_endpoint(conversation),
            {"media_url": _media_url(public_id), "media_public_id": public_id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["data"]["reason"], "invalid_media")

    def test_rejects_another_actors_chat_folder(self):
        conversation = self._mutual_conversation()
        # sender tries to claim a file in the receiver's chat folder
        public_id = f"chat/users/{self.receiver.id}/{uuid.uuid4()}"

        response = self.client.post(
            self._media_endpoint(conversation),
            {"media_url": _media_url(public_id), "media_public_id": public_id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["data"]["reason"], "invalid_media")

    def test_rejects_url_public_id_mismatch(self):
        conversation = self._mutual_conversation()
        signed = f"chat/users/{self.sender.id}/{uuid.uuid4()}"
        other = f"chat/users/{self.sender.id}/{uuid.uuid4()}"

        response = self.client.post(
            self._media_endpoint(conversation),
            {"media_url": _media_url(signed), "media_public_id": other},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["data"]["reason"], "invalid_media")

    def test_rejects_unsupported_format(self):
        conversation = self._mutual_conversation()

        response = self.client.post(
            self._media_endpoint(conversation),
            self._payload(sender_user=self.sender, ext="gif"),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["data"]["reason"], "invalid_media")

    def test_rejects_oversized_image(self):
        conversation = self._mutual_conversation()

        response = self.client.post(
            self._media_endpoint(conversation),
            self._payload(sender_user=self.sender, size_bytes=11 * 1024 * 1024),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["data"]["reason"], "invalid_media")

    def test_missing_conversation_is_404(self):
        missing = "00000000-0000-0000-0000-000000000000"

        response = self.client.post(
            f"/conversations/{missing}/messages/media",
            self._payload(sender_user=self.sender),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_requires_authentication(self):
        conversation = self._mutual_conversation()
        self.client.force_authenticate(user=None)

        response = self.client.post(
            self._media_endpoint(conversation),
            self._payload(sender_user=self.sender),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_service_rejects_foreign_url(self):
        conversation = self._mutual_conversation()
        public_id = f"chat/users/{self.sender.id}/{uuid.uuid4()}"

        with self.assertRaises(InvalidMediaError):
            MessageService.send_image_message(
                conversation=conversation,
                sender_user=self.sender,
                media_url=f"https://example.com/{public_id}.jpg",
                media_public_id=public_id,
            )

        self.assertEqual(Message.objects.count(), 0)


class ChatUploadSignatureTests(APITestCase):
    """The signature endpoint issues a chat-scoped config for both actors."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            email="sig@example.com", password="pass1234", username="siguser",
        )
        UserProfile.objects.create(user=self.user, name="Sig User")
        self.org = Organization.objects.create(
            name="Sig FC", username="sigfc", type=Organization.Type.CLUB,
        )
        OrganizationMember.objects.create(
            organization=self.org,
            user=self.user,
            role=OrganizationMember.Role.OWNER,
        )
        self.client.force_authenticate(user=self.user)

    def test_user_gets_chat_signature(self):
        response = self.client.get(
            "/user/get/upload/signature", {"type": "chat"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        upload = response.data["data"]["uploads"][0]
        self.assertTrue(
            upload["folder"].startswith(f"chat/users/{self.user.id}")
        )
        self.assertIn("signature", upload)

    def test_org_gets_chat_signature(self):
        response = self.client.get(
            "/user/get/upload/signature",
            {"type": "chat"},
            HTTP_X_ACTOR_TYPE="organization",
            HTTP_X_ACTOR_ID=str(self.org.id),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        upload = response.data["data"]["uploads"][0]
        self.assertTrue(
            upload["folder"].startswith(f"chat/organizations/{self.org.id}")
        )


def _video_url(public_id, ext="mp4"):
    return f"https://media.goatza.test/{public_id}.{ext}"


class VideoMessageTests(MessagingShareTestCase):
    """POST /conversations/<id>/messages/media with media_type=video."""

    def _media_endpoint(self, conversation):
        return f"/conversations/{conversation.id}/messages/media"

    def _payload(self, sender_user=None, sender_org=None, ext="mp4", **over):
        if sender_org is not None:
            public_id = f"chat/organizations/{sender_org.id}/{uuid.uuid4()}"
        else:
            owner = sender_user or self.sender
            public_id = f"chat/users/{owner.id}/{uuid.uuid4()}"

        body = {
            "media_type": "video",
            "media_url": _video_url(public_id, ext),
            "media_public_id": public_id,
            "width": 720,
            "height": 1280,
            "duration_ms": 8200,
            "size_bytes": 5_000_000,
            "caption": "",
        }
        body.update(over)
        return body

    # ── happy paths ──────────────────────────────────────────────

    def test_send_video_as_user_actor(self):
        conversation = self._mutual_conversation()

        response = self.client.post(
            self._media_endpoint(conversation),
            self._payload(sender_user=self.sender, caption="my goal"),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        message = Message.objects.get(conversation=conversation)
        self.assertEqual(message.message_type, Message.Type.VIDEO)
        self.assertEqual(message.sender_user_id, self.sender.id)
        self.assertEqual(message.content, "my goal")
        self.assertEqual(message.media_duration_ms, 8200)
        self.assertEqual(message.media_size_bytes, 5_000_000)

        # thumbnail derived server-side from the public_id (never client-sent)
        self.assertEqual(
            message.media_thumbnail_url,
            f"https://media.goatza.test/"
            f"{message.media_public_id}.jpg",
        )

        data = response.data["data"]
        self.assertEqual(data["message_type"], "video")
        self.assertEqual(data["media_duration_ms"], 8200)
        self.assertTrue(data["media_thumbnail_url"].endswith(".jpg"))

    def test_send_video_as_org_actor(self):
        conversation = self._conversation(
            actor_org=self.org, target_user=self.receiver
        )

        response = self.client.post(
            self._media_endpoint(conversation),
            self._payload(sender_org=self.org),
            format="json",
            **self._org_headers(),
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        message = Message.objects.get(conversation=conversation)
        self.assertEqual(message.sender_org_id, self.org.id)
        self.assertEqual(message.message_type, Message.Type.VIDEO)

    def test_video_updates_last_message_and_unread(self):
        conversation = self._mutual_conversation()

        self.client.post(
            self._media_endpoint(conversation),
            self._payload(sender_user=self.sender),
            format="json",
        )

        message = Message.objects.get(conversation=conversation)
        conversation.refresh_from_db()
        self.assertEqual(conversation.last_message_id, message.id)

        self.client.force_authenticate(user=self.receiver)
        summary = self.client.get("/conversations/unread/summary")
        self.assertEqual(summary.data["data"]["chats"], 1)

    def test_video_renders_in_message_list(self):
        conversation = self._mutual_conversation()
        self.client.post(
            self._media_endpoint(conversation),
            self._payload(sender_user=self.sender),
            format="json",
        )

        self.client.force_authenticate(user=self.receiver)
        response = self.client.get(
            MESSAGES_URL, {"conversation_id": str(conversation.id)}
        )

        message = response.data["data"]["results"][0]
        self.assertEqual(message["message_type"], "video")
        self.assertEqual(message["media_duration_ms"], 8200)
        self.assertTrue(message["media_thumbnail_url"])

    # ── rejections ───────────────────────────────────────────────

    def test_non_participant_cannot_send_video(self):
        conversation = self._conversation(
            actor_user=self.receiver, target_user=self.author
        )

        response = self.client.post(
            self._media_endpoint(conversation),
            self._payload(sender_user=self.sender),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data["data"]["reason"], "not_a_participant")
        self.assertEqual(Message.objects.count(), 0)

    def test_rejects_foreign_cloud(self):
        conversation = self._mutual_conversation()
        public_id = f"chat/users/{self.sender.id}/{uuid.uuid4()}"

        response = self.client.post(
            self._media_endpoint(conversation),
            self._payload(
                sender_user=self.sender,
                media_url=(
                    f"https://media.goatza.test/"
                    f"v1/{public_id}.mp4"
                ),
                media_public_id=public_id,
            ),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["data"]["reason"], "invalid_media")

    def test_rejects_another_actors_folder(self):
        conversation = self._mutual_conversation()
        public_id = f"chat/users/{self.receiver.id}/{uuid.uuid4()}"

        response = self.client.post(
            self._media_endpoint(conversation),
            self._payload(
                sender_user=self.sender,
                media_url=_video_url(public_id),
                media_public_id=public_id,
            ),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["data"]["reason"], "invalid_media")

    def test_rejects_unsupported_format(self):
        conversation = self._mutual_conversation()

        response = self.client.post(
            self._media_endpoint(conversation),
            self._payload(sender_user=self.sender, ext="avi"),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["data"]["reason"], "invalid_media")

    def test_rejects_video_over_ninety_seconds(self):
        conversation = self._mutual_conversation()

        response = self.client.post(
            self._media_endpoint(conversation),
            self._payload(sender_user=self.sender, duration_ms=91_000),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["data"]["reason"], "invalid_media")
        self.assertEqual(Message.objects.count(), 0)

    def test_accepts_video_at_exactly_ninety_seconds(self):
        conversation = self._mutual_conversation()

        response = self.client.post(
            self._media_endpoint(conversation),
            self._payload(sender_user=self.sender, duration_ms=90_000),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_rejects_oversized_video(self):
        conversation = self._mutual_conversation()

        response = self.client.post(
            self._media_endpoint(conversation),
            self._payload(
                sender_user=self.sender, size_bytes=101 * 1024 * 1024
            ),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["data"]["reason"], "invalid_media")

    def test_service_rejects_long_video(self):
        conversation = self._mutual_conversation()
        public_id = f"chat/users/{self.sender.id}/{uuid.uuid4()}"

        with self.assertRaises(InvalidMediaError):
            MessageService.send_video_message(
                conversation=conversation,
                sender_user=self.sender,
                media_url=_video_url(public_id),
                media_public_id=public_id,
                duration_ms=120_000,
            )

        self.assertEqual(Message.objects.count(), 0)

    # Eager video-derivative coverage removed: nothing is transcoded
    # server-side any more — the uploaded object is the clip that plays.


class DeleteMessageTests(MessagingShareTestCase):
    """DELETE /conversations/<id>/messages/<message_id> — unsend."""

    def _endpoint(self, conversation, message):
        return f"/conversations/{conversation.id}/messages/{message.id}"

    def _send(self, conversation, sender_user=None, sender_org=None, text="hi"):
        return MessageService.send_message(
            conversation=conversation,
            sender_user=sender_user if sender_org is None else None,
            sender_org=sender_org,
            content=text,
        )

    # ── happy paths ──────────────────────────────────────────────

    def test_sender_can_delete_own_message(self):
        conversation = self._mutual_conversation()
        message = self._send(conversation, sender_user=self.sender)

        res = self.client.delete(self._endpoint(conversation, message))

        self.assertEqual(res.status_code, 200)
        message.refresh_from_db()
        self.assertTrue(message.is_deleted)

    def test_deleted_message_disappears_from_the_list(self):
        conversation = self._mutual_conversation()
        keep = self._send(conversation, sender_user=self.sender, text="keep me")
        drop = self._send(conversation, sender_user=self.sender, text="drop me")

        self.client.delete(self._endpoint(conversation, drop))

        res = self.client.get(
            f"/conversations/messages/list?conversation_id={conversation.id}"
        )
        ids = [m["id"] for m in res.data["data"]["results"]]
        self.assertIn(str(keep.id), ids)
        self.assertNotIn(str(drop.id), ids)

    def test_last_message_falls_back_to_the_previous_one(self):
        conversation = self._mutual_conversation()
        first = self._send(conversation, sender_user=self.sender, text="first")
        last = self._send(conversation, sender_user=self.sender, text="last")
        conversation.refresh_from_db()
        self.assertEqual(conversation.last_message_id, last.id)

        self.client.delete(self._endpoint(conversation, last))

        conversation.refresh_from_db()
        self.assertEqual(conversation.last_message_id, first.id)

    def test_deleting_the_only_message_clears_last_message(self):
        conversation = self._mutual_conversation()
        only = self._send(conversation, sender_user=self.sender)

        self.client.delete(self._endpoint(conversation, only))

        conversation.refresh_from_db()
        self.assertIsNone(conversation.last_message_id)
        self.assertIsNone(conversation.last_message_at)

    def test_org_actor_can_delete_its_own_message(self):
        conversation = self._conversation(
            actor_org=self.org, target_user=self.receiver
        )
        message = self._send(conversation, sender_org=self.org)

        res = self.client.delete(
            self._endpoint(conversation, message), **self._org_headers()
        )

        self.assertEqual(res.status_code, 200)
        message.refresh_from_db()
        self.assertTrue(message.is_deleted)

    # ── permissions ──────────────────────────────────────────────

    def test_recipient_cannot_delete_someone_elses_message(self):
        conversation = self._mutual_conversation()
        message = self._send(conversation, sender_user=self.sender)

        self.client.force_authenticate(user=self.receiver)
        res = self.client.delete(self._endpoint(conversation, message))

        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["data"]["reason"], "not_message_sender")
        message.refresh_from_db()
        self.assertFalse(message.is_deleted)

    def test_acting_as_org_cannot_delete_your_personal_message(self):
        """The same human, but a different acting identity — still not theirs."""
        conversation = self._mutual_conversation()
        message = self._send(conversation, sender_user=self.sender)

        res = self.client.delete(
            self._endpoint(conversation, message), **self._org_headers()
        )

        self.assertEqual(res.status_code, 403)
        message.refresh_from_db()
        self.assertFalse(message.is_deleted)

    def test_outsider_cannot_delete(self):
        conversation = self._mutual_conversation()
        message = self._send(conversation, sender_user=self.sender)

        self.client.force_authenticate(user=self.author)
        res = self.client.delete(self._endpoint(conversation, message))

        self.assertEqual(res.status_code, 403)
        message.refresh_from_db()
        self.assertFalse(message.is_deleted)

    def test_requires_authentication(self):
        conversation = self._mutual_conversation()
        message = self._send(conversation, sender_user=self.sender)

        self.client.force_authenticate(user=None)
        res = self.client.delete(self._endpoint(conversation, message))

        self.assertIn(res.status_code, (401, 403))

    # ── not found ────────────────────────────────────────────────

    def test_deleting_twice_is_a_404(self):
        conversation = self._mutual_conversation()
        message = self._send(conversation, sender_user=self.sender)

        self.client.delete(self._endpoint(conversation, message))
        res = self.client.delete(self._endpoint(conversation, message))

        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.data["data"]["reason"], "message_not_found")

    def test_message_from_another_conversation_is_not_found(self):
        conversation = self._mutual_conversation()
        other = self._conversation(
            actor_user=self.sender, target_user=self.author
        )
        message = self._send(other, sender_user=self.sender)

        res = self.client.delete(self._endpoint(conversation, message))

        self.assertEqual(res.status_code, 404)
        message.refresh_from_db()
        self.assertFalse(message.is_deleted)

    def test_unknown_conversation_is_a_404(self):
        conversation = self._mutual_conversation()
        message = self._send(conversation, sender_user=self.sender)

        res = self.client.delete(
            f"/conversations/{uuid.uuid4()}/messages/{message.id}"
        )

        self.assertEqual(res.status_code, 404)
