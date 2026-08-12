"""
Deep-link tests for notifications.

These assert the exact URL string per type, for BOTH recipient shapes wherever a
type supports both. The strings matter literally: the web client navigates to
``url`` verbatim and reads the path to decide which actor the person is acting
as, so a wrong prefix here doesn't render a 404 — it switches someone out of
their organization.

``build_notification_url`` is called directly rather than through the API: the
resolver is where the rule lives, the view only paginates.
"""

import uuid

from django.test import TestCase, override_settings

from accounts.models import User, UserProfile
from notifications.models import Notification
from notifications.services.deeplink_service import (
    build_conversation_url,
    build_notification_url,
)
from notifications.services.notification_service import build_notification_payload
from organization.models import Organization, OrganizationProfile
from posts.models import Post
from recruitments.models import Recruitment
from sports.models import Sport


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"]
)
class NotificationDeepLinkTests(TestCase):
    """One player, one club, and one of every target a notification can point at."""

    def setUp(self):
        self.player = self._user("alice")
        self.owner = self._user("owner")

        self.club = self._org("dreamfc", "Dream FC")
        self.recipient_club = self._org("cityfc", "City FC")

        self.base = f"/organization/admin/{self.recipient_club.id}"

        self.sport = Sport.objects.create(name="Football", icon_name="mdi:soccer")
        self.post = Post.objects.create(author_user=self.player, content="hello")
        self.recruitment = Recruitment.objects.create(
            organization=self.recipient_club,
            sport=self.sport,
            title="U18 Trials",
            recruitment_type=Recruitment.Type.OPEN_TRIAL,
        )
        self.conversation_id = uuid.uuid4()

    # ── factories ────────────────────────────────────────────────

    def _user(self, username):
        user = User.objects.create_user(
            email=f"{username}@example.com",
            password="pass1234",
            username=username,
        )
        UserProfile.objects.create(user=user, name=username.title())
        return user

    def _org(self, username, name):
        org = Organization.objects.create(
            name=name,
            username=username,
            type=Organization.Type.CLUB,
        )
        OrganizationProfile.objects.create(organization=org)
        return org

    def _notif(self, ntype, *, to_user=None, to_org=None,
               by_user=None, by_org=None, **fields):
        """
        A saved notification. Exactly one recipient and one actor, matching the
        model's own check constraints — the resolver branches on which is set.
        """
        return Notification.objects.create(
            type=ntype,
            recipient_user=to_user,
            recipient_org=to_org,
            actor_user=by_user,
            actor_org=by_org,
            **fields,
        )

    # ── follow / follow_back ─────────────────────────────────────

    def test_follow_to_user_from_user(self):
        notif = self._notif(
            Notification.Type.FOLLOW, to_user=self.owner, by_user=self.player
        )
        self.assertEqual(build_notification_url(notif), "/profile/alice")

    def test_follow_to_user_from_org(self):
        notif = self._notif(
            Notification.Type.FOLLOW, to_user=self.owner, by_org=self.club
        )
        self.assertEqual(
            build_notification_url(notif), "/organization/profile/dreamfc"
        )

    def test_follow_to_org_from_user(self):
        notif = self._notif(
            Notification.Type.FOLLOW,
            to_org=self.recipient_club,
            by_user=self.player,
        )
        self.assertEqual(
            build_notification_url(notif), f"{self.base}/profile/user/alice"
        )

    def test_follow_to_org_from_org(self):
        notif = self._notif(
            Notification.Type.FOLLOW,
            to_org=self.recipient_club,
            by_org=self.club,
        )
        self.assertEqual(
            build_notification_url(notif), f"{self.base}/profile/org/dreamfc"
        )

    def test_follow_back_to_user_from_user(self):
        notif = self._notif(
            Notification.Type.FOLLOW_BACK, to_user=self.owner, by_user=self.player
        )
        self.assertEqual(build_notification_url(notif), "/profile/alice")

    def test_follow_back_to_user_from_org(self):
        notif = self._notif(
            Notification.Type.FOLLOW_BACK, to_user=self.owner, by_org=self.club
        )
        self.assertEqual(
            build_notification_url(notif), "/organization/profile/dreamfc"
        )

    def test_follow_back_to_org_from_user(self):
        notif = self._notif(
            Notification.Type.FOLLOW_BACK,
            to_org=self.recipient_club,
            by_user=self.player,
        )
        self.assertEqual(
            build_notification_url(notif), f"{self.base}/profile/user/alice"
        )

    def test_follow_back_to_org_from_org(self):
        notif = self._notif(
            Notification.Type.FOLLOW_BACK,
            to_org=self.recipient_club,
            by_org=self.club,
        )
        self.assertEqual(
            build_notification_url(notif), f"{self.base}/profile/org/dreamfc"
        )

    # ── post interactions ────────────────────────────────────────

    def test_like_to_user(self):
        notif = self._notif(
            Notification.Type.LIKE,
            to_user=self.owner,
            by_user=self.player,
            post=self.post,
        )
        self.assertEqual(build_notification_url(notif), f"/posts/{self.post.id}")

    def test_like_to_org(self):
        notif = self._notif(
            Notification.Type.LIKE,
            to_org=self.recipient_club,
            by_user=self.player,
            post=self.post,
        )
        self.assertEqual(
            build_notification_url(notif), f"{self.base}/posts/{self.post.id}"
        )

    def test_comment_to_user(self):
        notif = self._notif(
            Notification.Type.COMMENT,
            to_user=self.owner,
            by_user=self.player,
            post=self.post,
        )
        self.assertEqual(build_notification_url(notif), f"/posts/{self.post.id}")

    def test_comment_to_org(self):
        notif = self._notif(
            Notification.Type.COMMENT,
            to_org=self.recipient_club,
            by_user=self.player,
            post=self.post,
        )
        self.assertEqual(
            build_notification_url(notif), f"{self.base}/posts/{self.post.id}"
        )

    def test_mention_to_user(self):
        notif = self._notif(
            Notification.Type.MENTION,
            to_user=self.owner,
            by_user=self.player,
            post=self.post,
        )
        self.assertEqual(build_notification_url(notif), f"/posts/{self.post.id}")

    def test_mention_to_org(self):
        notif = self._notif(
            Notification.Type.MENTION,
            to_org=self.recipient_club,
            by_user=self.player,
            post=self.post,
        )
        self.assertEqual(
            build_notification_url(notif), f"{self.base}/posts/{self.post.id}"
        )

    # ── message ──────────────────────────────────────────────────

    def test_message_to_user(self):
        notif = self._notif(
            Notification.Type.MESSAGE,
            to_user=self.owner,
            by_user=self.player,
            data={"conversation_id": str(self.conversation_id)},
        )
        self.assertEqual(
            build_notification_url(notif),
            f"/messages/chat/{self.conversation_id}",
        )

    def test_message_to_org(self):
        notif = self._notif(
            Notification.Type.MESSAGE,
            to_org=self.recipient_club,
            by_user=self.player,
            data={"conversation_id": str(self.conversation_id)},
        )
        self.assertEqual(
            build_notification_url(notif),
            f"{self.base}/messages/chat/{self.conversation_id}",
        )

    # ── recruitment ──────────────────────────────────────────────

    def test_recruitment_application_to_org(self):
        notif = self._notif(
            Notification.Type.RECRUITMENT_APPLICATION,
            to_org=self.recipient_club,
            by_user=self.player,
            recruitment=self.recruitment,
        )
        self.assertEqual(
            build_notification_url(notif),
            f"{self.base}/recruitments/{self.recruitment.id}?tab=applicants",
        )

    def test_recruitment_application_status_to_user(self):
        notif = self._notif(
            Notification.Type.RECRUITMENT_APPLICATION_STATUS,
            to_user=self.player,
            by_org=self.recipient_club,
            recruitment=self.recruitment,
        )
        self.assertEqual(
            build_notification_url(notif),
            f"/recruitments/{self.recruitment.id}",
        )

    def test_career_add_prompt_to_user(self):
        # The row opens CareerAddPromptSheet in place, so the list IS the
        # destination — a recruitment deep link would leave the action behind.
        notif = self._notif(
            Notification.Type.CAREER_ADD_PROMPT,
            to_user=self.player,
            by_org=self.recipient_club,
            recruitment=self.recruitment,
            data={"application_id": str(uuid.uuid4())},
        )
        self.assertEqual(build_notification_url(notif), "/notifications")

    # ── verification queues (org side) ───────────────────────────

    def test_career_verification_request_to_org(self):
        notif = self._notif(
            Notification.Type.CAREER_VERIFICATION_REQUEST,
            to_org=self.recipient_club,
            by_user=self.player,
        )
        self.assertEqual(
            build_notification_url(notif), f"{self.base}/verifications"
        )

    def test_achievement_verification_request_to_org(self):
        notif = self._notif(
            Notification.Type.ACHIEVEMENT_VERIFICATION_REQUEST,
            to_org=self.recipient_club,
            by_user=self.player,
        )
        self.assertEqual(
            build_notification_url(notif),
            f"{self.base}/verifications?tab=achievements",
        )

    # ── verification decisions (owner side) ──────────────────────

    def test_career_verified_to_user(self):
        notif = self._notif(
            Notification.Type.CAREER_VERIFIED,
            to_user=self.player,
            by_org=self.recipient_club,
            data={"owner_username": "alice"},
        )
        self.assertEqual(build_notification_url(notif), "/profile/alice#career")

    def test_career_rejected_to_user(self):
        notif = self._notif(
            Notification.Type.CAREER_REJECTED,
            to_user=self.player,
            by_org=self.recipient_club,
            data={"owner_username": "alice"},
        )
        self.assertEqual(build_notification_url(notif), "/profile/alice#career")

    def test_achievement_verified_to_user(self):
        notif = self._notif(
            Notification.Type.ACHIEVEMENT_VERIFIED,
            to_user=self.player,
            by_org=self.recipient_club,
            data={"owner_username": "alice"},
        )
        self.assertEqual(
            build_notification_url(notif), "/profile/alice#achievements"
        )

    def test_achievement_rejected_to_user(self):
        notif = self._notif(
            Notification.Type.ACHIEVEMENT_REJECTED,
            to_user=self.player,
            by_org=self.recipient_club,
            data={"owner_username": "alice"},
        )
        self.assertEqual(
            build_notification_url(notif), "/profile/alice#achievements"
        )

    # ── fallbacks ────────────────────────────────────────────────

    def test_missing_post_falls_back_to_user_notifications(self):
        notif = self._notif(
            Notification.Type.LIKE, to_user=self.owner, by_user=self.player
        )
        self.assertEqual(build_notification_url(notif), "/notifications")

    def test_missing_post_falls_back_inside_the_org_space(self):
        # The point of the fallback: never "/" and never out of the admin space.
        notif = self._notif(
            Notification.Type.LIKE,
            to_org=self.recipient_club,
            by_user=self.player,
        )
        self.assertEqual(build_notification_url(notif), f"{self.base}/notifications")

    def test_missing_recruitment_falls_back(self):
        notif = self._notif(
            Notification.Type.RECRUITMENT_APPLICATION,
            to_org=self.recipient_club,
            by_user=self.player,
        )
        self.assertEqual(build_notification_url(notif), f"{self.base}/notifications")

    def test_missing_owner_username_falls_back(self):
        notif = self._notif(
            Notification.Type.CAREER_VERIFIED,
            to_user=self.player,
            by_org=self.recipient_club,
        )
        self.assertEqual(build_notification_url(notif), "/notifications")

    # ── shared conversation helper ───────────────────────────────

    def test_conversation_url_is_the_chat_route_not_the_username_route(self):
        self.assertEqual(
            build_conversation_url(self.conversation_id),
            f"/messages/chat/{self.conversation_id}",
        )
        self.assertEqual(
            build_conversation_url(self.conversation_id, self.recipient_club.id),
            f"{self.base}/messages/chat/{self.conversation_id}",
        )

    # ── push payload ─────────────────────────────────────────────

    def test_payload_carries_the_resolved_url_and_actor_shape(self):
        notif = self._notif(
            Notification.Type.LIKE,
            to_org=self.recipient_club,
            by_user=self.player,
            post=self.post,
        )
        payload = build_notification_payload(notif)

        self.assertEqual(payload["url"], f"{self.base}/posts/{self.post.id}")
        self.assertEqual(payload["actor_type"], "user")
        self.assertEqual(payload["recipient_type"], "organization")
        self.assertEqual(payload["recipient_org_id"], str(self.recipient_club.id))

    def test_payload_recipient_org_id_is_blank_for_a_user(self):
        notif = self._notif(
            Notification.Type.FOLLOW, to_user=self.owner, by_org=self.club
        )
        payload = build_notification_payload(notif)

        self.assertEqual(payload["actor_type"], "organization")
        self.assertEqual(payload["recipient_type"], "user")
        self.assertEqual(payload["recipient_org_id"], "")
