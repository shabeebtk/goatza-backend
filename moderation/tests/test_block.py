"""
The block feature, end to end.

Organised the way the feature was built, because each layer only makes sense
once the one under it holds:

  MODEL       the four partial uniques and the two exactly-one constraints —
              the things that stay true even if every service is bypassed
  SERVICE     block/unblock, the follow teardown and its counters, the org
              role gate, and the cached blocked_ids set
  GUARDS      the seven write paths, each proved to refuse a blocked pair AND
              to still work for a normal one (a guard that refuses everything
              passes a one-sided test)
  READS       the listing surfaces, always with a third account C present:
              "B is gone" is only interesting next to "C still sees B"
  PROFILE     §1.5 — the shell-plus-flag state, and the 404 that must be
              byte-identical to an unknown username's
  ENDPOINTS   auth, actor headers, pagination, bad input

The load-bearing test in the file is ``test_blocked_profile_404_is_byte_
identical_to_unknown``: everything else can be re-derived from the code, but a
404 that differs from the unknown-username 404 by a single word hands a prober
the one bit the whole feature exists to withhold.
"""

import json

from asgiref.sync import async_to_sync

from channels.testing import WebsocketCommunicator
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase, APITransactionTestCase

from accounts.models import User, UserProfile
from connections.models import Follow
from connections.services.follow_services import FollowService
from core.actor import Actor
from highlights.models import Highlight
from messaging.consumers.chat_consumers import ChatConsumer
from messaging.models import (
    Conversation,
    ConversationParticipant,
    Message,
)
from messaging.services.conversation_service import ConversationService
from messaging.services.exceptions import BlockedParticipantError
from messaging.services.message_service import MessageService
from moderation.models import Block
from moderation.selectors.blocked_filters import exclude_blocked
from moderation.services.block_guard import BLOCKED_MESSAGE, BlockedError
from moderation.services.block_services import BlockService
from notifications.models import Notification
from organization.models import (
    Organization,
    OrganizationMember,
    OrganizationProfile,
)
from posts.models import Comment, Post, PostMention
from recruitments.models import Recruitment, RecruitmentApplication
from sports.models import Sport
from usernames.services.username_service import UsernameService
from utils.cache_keys import CacheKeys
from legal.testing import accept_current_terms

BLOCK_URL = "/moderation/block"
BLOCKED_URL = "/moderation/blocked"
FOLLOW_URL = "/connections/user/follow"
COMMENT_URL = "/posts/comments/create"
LIKE_URL = "/posts/like"
SHARE_URL = "/conversations/share"
CONVERSATION_URL = "/conversations/get-or-create"

# Redis is not available in tests and every send fans out over the channel
# layer. Same override the messaging suite uses.
CHANNEL_LAYERS = {
    "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}
}


@override_settings(
    CHANNEL_LAYERS=CHANNEL_LAYERS,
    # Five-plus users per test; the real hasher makes setUp dominate the run.
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class BlockTestBase(APITestCase):
    """
    Shared cast: A blocks B. C is the control and blocks nobody.

    Every read-side assertion is a triple — absent for A, absent for B,
    PRESENT for C — because a filter that drops the row for everyone would
    satisfy the first two on its own.
    """

    def setUp(self):
        # blocked_ids is cached, and the cache outlives a test.
        cache.clear()

        self.sport = Sport.objects.create(name="Football", icon_name="mdi:soccer")

        self.a = self._user("alpha")
        self.b = self._user("bravo")
        self.c = self._user("charlie")

        self.owner = self._user("clubowner", User.Role.ORG_USER)
        self.admin = self._user("clubadmin", User.Role.ORG_USER)
        self.coach = self._user("clubcoach", User.Role.COACH)

        self.org = self._org("dreamfc", "Dream FC")
        self.other_org = self._org("rivalfc", "Rival FC")

        self.owner_member = OrganizationMember.objects.create(
            organization=self.org, user=self.owner,
            role=OrganizationMember.Role.OWNER,
        )
        self.admin_member = OrganizationMember.objects.create(
            organization=self.org, user=self.admin,
            role=OrganizationMember.Role.ADMIN,
        )
        self.coach_member = OrganizationMember.objects.create(
            organization=self.org, user=self.coach,
            role=OrganizationMember.Role.COACH,
        )

        self.a_actor = Actor(actor_type="user", user=self.a)
        self.b_actor = Actor(actor_type="user", user=self.b)
        self.c_actor = Actor(actor_type="user", user=self.c)

    # ---------------- fixtures ----------------

    def _user(self, username, role=User.Role.PLAYER):
        user = User.objects.create_user(
            email=f"{username}@example.com",
            password="pass1234",
            username=username,
            role=role,
        )
        accept_current_terms(user)
        UserProfile.objects.create(user=user, name=username.title())
        UsernameService.claim(username, user=user)
        return user

    def _org(self, username, name):
        org = Organization.objects.create(
            name=name, username=username, type=Organization.Type.CLUB
        )
        OrganizationProfile.objects.create(organization=org)
        UsernameService.claim(username, organization=org)
        return org

    def _org_actor(self, member):
        return Actor(
            actor_type="organization",
            organization=member.organization,
            organization_member=member,
        )

    def _post(self, author, content="a post about football"):
        kwargs = {"content": content, "sport": self.sport,
                  "visibility": Post.Visibility.PUBLIC}
        if isinstance(author, User):
            return Post.objects.create(author_user=author, **kwargs)
        return Post.objects.create(author_org=author, **kwargs)

    def _recruitment(self, org):
        return Recruitment.objects.create(
            organization=org, sport=self.sport, title="Trials",
            short_description="open trials",
            recruitment_type=Recruitment.Type.OPEN_TRIAL,
            status=Recruitment.Status.ACTIVE,
            visibility=Recruitment.Visibility.PUBLIC,
        )

    def _conversation(self, user_x, user_y, accept=True, message="hi"):
        convo, _ = ConversationService.get_or_create_conversation(
            actor_user=user_x, target_user=user_y
        )
        MessageService.send_message(
            conversation=convo, sender_user=user_x, content=message
        )
        if accept:
            ConversationService.accept_conversation(
                convo, actor_user=user_y, actor_org=None
            )
        return convo

    def _auth(self, user, org=None):
        self.client.force_authenticate(user=user)
        if org is None:
            return {}
        return {
            "HTTP_X_ACTOR_TYPE": "organization",
            "HTTP_X_ACTOR_ID": str(org.id),
        }

    def _block(self, actor, target_user=None, target_org=None):
        return BlockService.block(
            actor=actor, target_user=target_user, target_org=target_org
        )

    # ---------------- assertions ----------------

    def assertBlockedEnvelope(self, response):
        """The standard 403 body, and nothing in it about who blocked whom."""
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["message"], BLOCKED_MESSAGE)
        self.assertEqual(
            body["data"]["errors"]["non_field_errors"], BLOCKED_MESSAGE
        )
        # No direction, no identity, no "block" wording beyond the generic line.
        self.assertNotIn("blocked you", json.dumps(body).lower())


# =====================================================================
# MODEL
# =====================================================================

class BlockModelTests(BlockTestBase):

    def test_exactly_one_blocker_side(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Block.objects.create(
                    blocker_user=self.a, blocker_org=self.org,
                    blocked_user=self.b,
                )

    def test_blocker_side_cannot_be_empty(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Block.objects.create(blocked_user=self.b)

    def test_exactly_one_blocked_side(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Block.objects.create(
                    blocker_user=self.a,
                    blocked_user=self.b, blocked_org=self.org,
                )

    def test_blocked_side_cannot_be_empty(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Block.objects.create(blocker_user=self.a)

    def test_self_block_rejected_user(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Block.objects.create(blocker_user=self.a, blocked_user=self.a)

    def test_self_block_rejected_org(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Block.objects.create(blocker_org=self.org, blocked_org=self.org)

    def test_cross_type_block_is_allowed(self):
        """
        user -> org must NOT trip the self-block check. The constraint is two
        column comparisons AND-ed, and each is NULL for a cross-type row —
        exactly the case a naive `exclude(a=b, c=d)` would get wrong.
        """
        Block.objects.create(blocker_user=self.a, blocked_org=self.org)
        Block.objects.create(blocker_org=self.org, blocked_user=self.a)
        self.assertEqual(Block.objects.count(), 2)

    def test_duplicate_rejected_for_all_four_identity_pairs(self):
        pairs = (
            ("user->user", {"blocker_user": self.a, "blocked_user": self.b}),
            ("user->org", {"blocker_user": self.a, "blocked_org": self.org}),
            ("org->user", {"blocker_org": self.org, "blocked_user": self.b}),
            ("org->org", {"blocker_org": self.org, "blocked_org": self.other_org}),
        )
        for label, kwargs in pairs:
            with self.subTest(pair=label):
                Block.objects.create(**kwargs)
                with self.assertRaises(IntegrityError):
                    with transaction.atomic():
                        Block.objects.create(**kwargs)

    def test_partial_uniques_do_not_collide_across_pairs(self):
        """
        The four uniques are partial. Without the conditions, two rows whose
        blocked_org is NULL would collide on (blocker_user, blocked_org).
        """
        Block.objects.create(blocker_user=self.a, blocked_user=self.b)
        Block.objects.create(blocker_user=self.a, blocked_user=self.c)
        self.assertEqual(Block.objects.count(), 2)


# =====================================================================
# SERVICE
# =====================================================================

class BlockServiceTests(BlockTestBase):

    def test_block_creates_row(self):
        ok, result = self._block(self.a_actor, target_user=self.b)
        self.assertTrue(ok)
        self.assertTrue(result["is_blocked"])
        self.assertFalse(result["already_blocked"])
        self.assertTrue(
            Block.objects.filter(blocker_user=self.a, blocked_user=self.b).exists()
        )

    def test_block_removes_follows_in_both_directions(self):
        FollowService.follow(actor=self.a_actor, target_user=self.b)
        FollowService.follow(actor=self.b_actor, target_user=self.a)
        self.assertEqual(Follow.objects.count(), 2)

        self._block(self.a_actor, target_user=self.b)

        self.assertEqual(Follow.objects.count(), 0)

    def test_block_leaves_counters_correct(self):
        FollowService.follow(actor=self.a_actor, target_user=self.b)
        FollowService.follow(actor=self.b_actor, target_user=self.a)

        # mutual -> both connected before the block
        self.assertEqual(self._counters(self.a)["connections_count"], 1)

        self._block(self.a_actor, target_user=self.b)

        for user in (self.a, self.b):
            with self.subTest(user=user.username):
                counters = self._counters(user)
                self.assertEqual(counters["followers_count"], 0)
                self.assertEqual(counters["following_count"], 0)
                self.assertEqual(counters["connections_count"], 0)

    def test_block_does_not_touch_unrelated_follows(self):
        FollowService.follow(actor=self.a_actor, target_user=self.b)
        FollowService.follow(actor=self.a_actor, target_user=self.c)

        self._block(self.a_actor, target_user=self.b)

        self.assertTrue(
            Follow.objects.filter(follower_user=self.a, following_user=self.c).exists()
        )
        self.assertEqual(self._counters(self.a)["following_count"], 1)

    def test_block_is_idempotent(self):
        self._block(self.a_actor, target_user=self.b)
        ok, result = self._block(self.a_actor, target_user=self.b)

        self.assertTrue(ok)
        self.assertTrue(result["already_blocked"])
        self.assertEqual(Block.objects.count(), 1)

    def test_block_creates_no_notification(self):
        before = Notification.objects.count()
        self._block(self.a_actor, target_user=self.b)
        self.assertEqual(Notification.objects.count(), before)

    def test_unfollow_side_effect_creates_no_notification(self):
        """
        The teardown runs FollowService.unfollow, which is notification-free —
        but this pins it, because a future 'someone unfollowed you' feature
        would otherwise leak the block.
        """
        FollowService.follow(actor=self.a_actor, target_user=self.b)
        before = Notification.objects.count()

        self._block(self.a_actor, target_user=self.b)

        self.assertEqual(Notification.objects.count(), before)

    def test_self_block_refused(self):
        ok, message = self._block(self.a_actor, target_user=self.a)
        self.assertFalse(ok)
        self.assertEqual(message, "Cannot block yourself")

    # ---- org role gate ----

    def test_org_owner_may_block(self):
        ok, _ = self._block(self._org_actor(self.owner_member), target_user=self.b)
        self.assertTrue(ok)

    def test_org_admin_may_block(self):
        ok, _ = self._block(self._org_actor(self.admin_member), target_user=self.b)
        self.assertTrue(ok)

    def test_org_coach_may_not_block(self):
        with self.assertRaises(BlockedError.__mro__[1]):  # PermissionDenied
            self._block(self._org_actor(self.coach_member), target_user=self.b)
        self.assertEqual(Block.objects.count(), 0)

    def test_org_staff_may_not_block(self):
        member = OrganizationMember.objects.create(
            organization=self.other_org, user=self.c,
            role=OrganizationMember.Role.STAFF,
        )
        with self.assertRaises(BlockedError.__mro__[1]):
            self._block(self._org_actor(member), target_user=self.b)

    # ---- unblock ----

    def test_unblock_removes_row(self):
        self._block(self.a_actor, target_user=self.b)
        ok, result = BlockService.unblock(self.a_actor, target_user=self.b)

        self.assertTrue(ok)
        self.assertTrue(result["was_blocked"])
        self.assertEqual(Block.objects.count(), 0)

    def test_unblock_does_not_restore_follows(self):
        FollowService.follow(actor=self.a_actor, target_user=self.b)
        FollowService.follow(actor=self.b_actor, target_user=self.a)

        self._block(self.a_actor, target_user=self.b)
        BlockService.unblock(self.a_actor, target_user=self.b)

        self.assertEqual(Follow.objects.count(), 0)
        self.assertEqual(self._counters(self.a)["following_count"], 0)

    def test_unblock_is_idempotent(self):
        ok, result = BlockService.unblock(self.a_actor, target_user=self.b)
        self.assertTrue(ok)
        self.assertFalse(result["was_blocked"])

    # ---- blocked_ids + cache ----

    def test_blocked_ids_contains_both_directions(self):
        self._block(self.a_actor, target_user=self.b)

        self.assertIn(self.b.id, BlockService.blocked_ids(self.a_actor)["user_ids"])
        self.assertIn(self.a.id, BlockService.blocked_ids(self.b_actor)["user_ids"])

    def test_blocked_ids_separates_users_and_orgs(self):
        self._block(self.a_actor, target_user=self.b)
        self._block(self.a_actor, target_org=self.org)

        ids = BlockService.blocked_ids(self.a_actor)
        self.assertEqual(ids["user_ids"], {self.b.id})
        self.assertEqual(ids["org_ids"], {self.org.id})

    def test_blocked_ids_empty_for_uninvolved_actor(self):
        self._block(self.a_actor, target_user=self.b)
        ids = BlockService.blocked_ids(self.c_actor)
        self.assertEqual(ids["user_ids"], set())
        self.assertEqual(ids["org_ids"], set())

    def test_blocked_ids_is_cached(self):
        BlockService.blocked_ids(self.a_actor)
        self.assertIsNotNone(cache.get(CacheKeys.blocked_ids("user", self.a.id)))

    def test_block_invalidates_cache_for_both_parties(self):
        # warm both
        BlockService.blocked_ids(self.a_actor)
        BlockService.blocked_ids(self.b_actor)

        self._block(self.a_actor, target_user=self.b)

        # asserted through the cache utils, not by waiting out a TTL
        self.assertIsNone(cache.get(CacheKeys.blocked_ids("user", self.a.id)))
        self.assertIsNone(cache.get(CacheKeys.blocked_ids("user", self.b.id)))

    def test_unblock_invalidates_cache_for_both_parties(self):
        self._block(self.a_actor, target_user=self.b)
        BlockService.blocked_ids(self.a_actor)
        BlockService.blocked_ids(self.b_actor)

        BlockService.unblock(self.a_actor, target_user=self.b)

        self.assertIsNone(cache.get(CacheKeys.blocked_ids("user", self.a.id)))
        self.assertIsNone(cache.get(CacheKeys.blocked_ids("user", self.b.id)))

    def test_is_blocked_between_is_symmetric(self):
        self._block(self.a_actor, target_user=self.b)
        self.assertTrue(
            BlockService.is_blocked_between(self.a_actor, target_user=self.b)
        )
        self.assertTrue(
            BlockService.is_blocked_between(self.b_actor, target_user=self.a)
        )
        self.assertFalse(
            BlockService.is_blocked_between(self.c_actor, target_user=self.b)
        )

    def test_is_blocked_between_directed_is_one_way(self):
        self._block(self.a_actor, target_user=self.b)
        self.assertTrue(
            BlockService.is_blocked_between_directed(self.a_actor, target_user=self.b)
        )
        self.assertFalse(
            BlockService.is_blocked_between_directed(self.b_actor, target_user=self.a)
        )

    def _counters(self, user):
        profile = UserProfile.objects.get(user=user)
        return {
            "followers_count": profile.followers_count,
            "following_count": profile.following_count,
            "connections_count": profile.connections_count,
        }


# =====================================================================
# WRITE GUARDS
# =====================================================================

class BlockGuardTests(BlockTestBase):

    def setUp(self):
        super().setUp()
        self._block(self.a_actor, target_user=self.b)

    # ---- follow ----

    def test_follow_blocked_pair_refused(self):
        self._auth(self.b)
        resp = self.client.post(
            FOLLOW_URL,
            {"target_type": "user", "target_id": str(self.a.id)},
            format="json",
        )
        self.assertBlockedEnvelope(resp)
        self.assertEqual(Follow.objects.count(), 0)

    def test_follow_refused_in_the_other_direction_too(self):
        self._auth(self.a)
        resp = self.client.post(
            FOLLOW_URL,
            {"target_type": "user", "target_id": str(self.b.id)},
            format="json",
        )
        self.assertBlockedEnvelope(resp)

    def test_follow_normal_pair_succeeds(self):
        self._auth(self.a)
        resp = self.client.post(
            FOLLOW_URL,
            {"target_type": "user", "target_id": str(self.c.id)},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.json()["success"])
        self.assertTrue(
            Follow.objects.filter(follower_user=self.a, following_user=self.c).exists()
        )

    # ---- send message ----

    def test_send_message_blocked_pair_refused(self):
        convo = self._conversation(self.a, self.c)
        # re-point: build a thread with B before the block would be cleaner,
        # but a thread can also predate the block — that is the real case.
        blocked_convo = Conversation.objects.create(type=Conversation.Type.DIRECT)
        from messaging.models import ConversationParticipant
        ConversationParticipant.objects.create(
            conversation=blocked_convo, user=self.a, has_accepted=True
        )
        ConversationParticipant.objects.create(
            conversation=blocked_convo, user=self.b, has_accepted=True
        )

        before = Message.objects.count()
        with self.assertRaises(BlockedParticipantError) as ctx:
            MessageService.send_message(
                conversation=blocked_convo, sender_user=self.a, content="hello?"
            )
        self.assertEqual(ctx.exception.reason, "blocked")
        self.assertEqual(str(ctx.exception), BLOCKED_MESSAGE)
        self.assertEqual(Message.objects.count(), before)
        self.assertIsNotNone(convo)

    def test_send_message_normal_pair_succeeds(self):
        convo = self._conversation(self.a, self.c)
        message = MessageService.send_message(
            conversation=convo, sender_user=self.a, content="still fine"
        )
        self.assertTrue(Message.objects.filter(id=message.id).exists())

    # ---- start conversation ----

    def test_start_conversation_blocked_pair_refused(self):
        self._auth(self.b)
        resp = self.client.post(
            CONVERSATION_URL, {"username": "alpha"}, format="json"
        )
        self.assertBlockedEnvelope(resp)

    def test_start_conversation_normal_pair_succeeds(self):
        self._auth(self.b)
        resp = self.client.post(
            CONVERSATION_URL, {"username": "charlie"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    # ---- comment / reaction ----

    def test_comment_on_blocked_authors_post_refused(self):
        post = self._post(self.a)
        self._auth(self.b)
        resp = self.client.post(
            COMMENT_URL,
            {"post_id": str(post.id), "comment": "hey"},
            format="json",
        )
        self.assertBlockedEnvelope(resp)
        post.refresh_from_db()
        self.assertEqual(post.comments_count, 0)

    def test_comment_on_normal_post_succeeds(self):
        post = self._post(self.c)
        self._auth(self.b)
        resp = self.client.post(
            COMMENT_URL,
            {"post_id": str(post.id), "comment": "hey"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_reaction_on_blocked_authors_post_refused(self):
        post = self._post(self.a)
        self._auth(self.b)
        resp = self.client.post(LIKE_URL, {"post_id": str(post.id)}, format="json")
        self.assertBlockedEnvelope(resp)
        post.refresh_from_db()
        self.assertEqual(post.likes_count, 0)

    def test_reaction_on_normal_post_succeeds(self):
        post = self._post(self.c)
        self._auth(self.b)
        resp = self.client.post(LIKE_URL, {"post_id": str(post.id)}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    # ---- mentions: silent, never an error ----

    def test_mention_of_blocking_party_is_silently_dropped(self):
        from posts.services.post_content_service import sync_post_content

        post = self._post(self.b, content="shout out @alpha and @charlie")
        added = sync_post_content(post)
        mentioned = {getattr(target, "username", None) for _, target in added}

        # the post itself survives
        self.assertTrue(Post.objects.filter(id=post.id).exists())
        self.assertNotIn("alpha", mentioned)
        self.assertIn("charlie", mentioned)
        self.assertFalse(
            PostMention.objects.filter(post=post, mentioned_user=self.a).exists()
        )
        self.assertTrue(
            PostMention.objects.filter(post=post, mentioned_user=self.c).exists()
        )

    def test_mention_of_blocking_party_creates_no_notification(self):
        from posts.services.post_content_service import sync_post_content

        before = Notification.objects.filter(
            type=Notification.Type.MENTION
        ).count() if hasattr(Notification.Type, "MENTION") else 0

        post = self._post(self.b, content="hi @alpha")
        sync_post_content(post)

        after = Notification.objects.filter(
            type=Notification.Type.MENTION
        ).count() if hasattr(Notification.Type, "MENTION") else 0
        self.assertEqual(after, before)

    # ---- recruitment application ----

    def test_apply_to_blocking_org_refused(self):
        org_actor = self._org_actor(self.owner_member)
        self._block(org_actor, target_user=self.b)
        recruitment = self._recruitment(self.org)

        self._auth(self.b)
        resp = self.client.post(
            f"/recruitments/{recruitment.id}/apply",
            {"shared_name": "Bravo", "shared_phone": "9876543210"},
            format="json",
        )
        self.assertBlockedEnvelope(resp)
        self.assertFalse(
            RecruitmentApplication.objects.filter(recruitment=recruitment).exists()
        )

    def test_apply_to_normal_org_succeeds(self):
        recruitment = self._recruitment(self.other_org)
        self._auth(self.b)
        resp = self.client.post(
            f"/recruitments/{recruitment.id}/apply",
            {"shared_name": "Bravo", "shared_phone": "9876543210"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(
            RecruitmentApplication.objects.filter(recruitment=recruitment).exists()
        )

    # ---- share to chat ----

    def test_share_reports_blocked_recipient_and_delivers_the_rest(self):
        post = self._post(self.a)
        self._auth(self.a)
        resp = self.client.post(
            SHARE_URL,
            {
                "target": {"type": "post", "id": str(post.id)},
                "recipients": [
                    {"actor_type": "user", "actor_id": str(self.b.id)},
                    {"actor_type": "user", "actor_id": str(self.c.id)},
                ],
            },
            format="json",
        )
        # Partial success is still 200 — the per-target contract.
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()["data"]
        self.assertEqual(len(data["sent"]), 1)
        self.assertEqual(
            data["failed"], [{"id": str(self.b.id), "reason": "blocked"}]
        )


# =====================================================================
# READ-SIDE EXCLUSIONS
# =====================================================================

class BlockReadExclusionTests(BlockTestBase):

    def setUp(self):
        super().setUp()
        self.post_a = self._post(self.a, "alpha on football")
        self.post_b = self._post(self.b, "bravo on football")
        self.post_c = self._post(self.c, "charlie on football")
        self._block(self.a_actor, target_user=self.b)

    def _ids(self, response, key="results"):
        data = response.json()["data"]
        rows = data if isinstance(data, list) else (data.get(key) or [])
        return {str(row.get("id")) for row in rows}

    def test_feed_excludes_the_blocked_pair_for_both_and_keeps_c(self):
        cases = (
            (self.a, str(self.post_b.id)),
            (self.b, str(self.post_a.id)),
        )
        for viewer, hidden in cases:
            with self.subTest(viewer=viewer.username):
                self._auth(viewer)
                ids = self._ids(self.client.get("/feed/list"))
                self.assertNotIn(hidden, ids)
                self.assertIn(str(self.post_c.id), ids)

        self._auth(self.c)
        ids = self._ids(self.client.get("/feed/list"))
        self.assertIn(str(self.post_a.id), ids)
        self.assertIn(str(self.post_b.id), ids)

    def test_explore_posts_excludes_blocked_author(self):
        self._auth(self.a)
        self.assertNotIn(
            str(self.post_b.id), self._ids(self.client.get("/feed/explore/posts"))
        )
        self._auth(self.c)
        self.assertIn(
            str(self.post_b.id), self._ids(self.client.get("/feed/explore/posts"))
        )

    def test_discover_players_excludes_blocked_identity_both_ways(self):
        self._auth(self.a)
        self.assertNotIn(
            str(self.b.id), self._ids(self.client.get("/feed/explore/players"))
        )
        self._auth(self.b)
        self.assertNotIn(
            str(self.a.id), self._ids(self.client.get("/feed/explore/players"))
        )
        self._auth(self.c)
        ids = self._ids(self.client.get("/feed/explore/players"))
        self.assertIn(str(self.a.id), ids)
        self.assertIn(str(self.b.id), ids)

    def test_discover_organizations_excludes_blocked_org(self):
        self._block(self.a_actor, target_org=self.org)
        self._auth(self.a)
        self.assertNotIn(
            str(self.org.id),
            self._ids(self.client.get("/feed/explore/organizations")),
        )
        self._auth(self.c)
        self.assertIn(
            str(self.org.id),
            self._ids(self.client.get("/feed/explore/organizations")),
        )

    def test_post_search_excludes_blocked_author(self):
        self._auth(self.a)
        ids = self._ids(self.client.get("/posts/search", {"q": "football"}))
        self.assertNotIn(str(self.post_b.id), ids)
        self.assertIn(str(self.post_c.id), ids)

        self._auth(self.c)
        ids = self._ids(self.client.get("/posts/search", {"q": "football"}))
        self.assertIn(str(self.post_b.id), ids)

    def test_comments_hidden_for_the_pair_but_not_for_a_third_party(self):
        for author in (self.a, self.b, self.c):
            Comment.objects.create(
                post=self.post_c, user=author, comment=f"from {author.username}"
            )

        expectations = (
            (self.a, "bravo", "charlie"),
            (self.b, "alpha", "charlie"),
        )
        for viewer, hidden, visible in expectations:
            with self.subTest(viewer=viewer.username):
                self._auth(viewer)
                resp = self.client.get(
                    "/posts/comments/list", {"post_id": str(self.post_c.id)}
                )
                authors = {
                    (row.get("actor") or {}).get("username")
                    for row in resp.json()["data"]["results"]
                }
                self.assertNotIn(hidden, authors)
                self.assertIn(visible, authors)

        # v1 rule: a third party's view of the same thread is untouched.
        self._auth(self.c)
        resp = self.client.get(
            "/posts/comments/list", {"post_id": str(self.post_c.id)}
        )
        authors = {
            (row.get("actor") or {}).get("username")
            for row in resp.json()["data"]["results"]
        }
        self.assertEqual(authors, {"alpha", "bravo", "charlie"})

    def test_recruitment_discover_excludes_blocked_org(self):
        from recruitments.services.discover_service import SECTION_ORDER

        self._block(self.a_actor, target_org=self.org)
        recruitment = self._recruitment(self.org)

        def discover_ids(user):
            self._auth(user)
            body = self.client.get("/recruitments/discover").json()["data"]
            return {
                str(row["id"])
                for section in SECTION_ORDER
                for row in (body.get(section) or [])
            }

        self.assertNotIn(str(recruitment.id), discover_ids(self.a))
        cache.clear()  # discover caches per actor
        self.assertIn(str(recruitment.id), discover_ids(self.c))

    def test_follow_lists_hide_the_blocked_identity(self):
        Follow.objects.create(follower_user=self.c, following_user=self.a)
        Follow.objects.create(follower_user=self.c, following_user=self.b)

        self._auth(self.a)
        resp = self.client.get(
            "/connections/user/follow/list",
            {"type": "following", "username": "charlie"},
        )
        names = {row["username"] for row in resp.json()["data"]["results"]}
        self.assertNotIn("bravo", names)
        self.assertIn("alpha", names)

        self._auth(self.c)
        resp = self.client.get(
            "/connections/user/follow/list",
            {"type": "following", "username": "charlie"},
        )
        names = {row["username"] for row in resp.json()["data"]["results"]}
        self.assertEqual(names, {"alpha", "bravo"})

    def test_empty_blocklist_leaves_the_queryset_untouched(self):
        """
        The fast path returns the SAME object — not an equivalent clone — so a
        page with no blocks pays nothing, not even a queryset rebuild.
        """
        base = Post.objects.filter(is_deleted=False)
        result = exclude_blocked(base, self.c_actor)

        self.assertIs(result, base)
        self.assertEqual(str(result.query), str(base.query))

    def test_non_empty_blocklist_adds_one_or_ed_exclusion(self):
        # BOTH sides blocked, so both halves of the condition are present —
        # with only a user blocked there is a single Q and nothing to OR.
        self._block(self.a_actor, target_org=self.org)
        cache.clear()

        base = Post.objects.filter(is_deleted=False)
        result = exclude_blocked(base, self.a_actor)

        self.assertIsNot(result, base)
        sql = str(result.query)
        self.assertIn("NOT", sql)
        # OR-ed, not AND-ed: an AND would exclude nothing on dual-actor rows.
        self.assertIn(" OR ", sql)

    def test_anonymous_actor_is_a_no_op(self):
        base = Post.objects.filter(is_deleted=False)
        self.assertIs(exclude_blocked(base, None), base)

    def test_duck_typed_actor_is_accepted(self):
        """
        The codebase passes actor-SHAPED objects that are not core.actor.Actor
        (feed ranking, and the feed suite's stub). An isinstance check here
        used to fall through to the org branch and raise AttributeError.
        """
        stub = type("StubActor", (), {
            "is_user": True, "is_org": False,
            "user": self.a, "organization": None,
        })()
        result = exclude_blocked(Post.objects.all(), stub)
        self.assertNotIn(self.post_b, list(result))


# =====================================================================
# PROFILE + CONVERSATION STATES (§1.5)
# =====================================================================

class BlockProfileStateTests(BlockTestBase):

    def setUp(self):
        super().setUp()
        self._block(self.a_actor, target_user=self.b)

    def test_blocker_sees_shell_with_flag(self):
        self._auth(self.a)
        resp = self.client.get("/user/bravo/details")

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()["data"]
        self.assertTrue(data["is_blocked_by_me"])
        self.assertEqual(data["username"], "bravo")

    def test_relationship_carries_block_state(self):
        self._auth(self.a)
        relationship = self.client.get(
            "/user/bravo/details"
        ).json()["data"]["relationship"]

        self.assertTrue(relationship["is_blocked"])
        self.assertTrue(relationship["is_blocked_by_me"])
        self.assertFalse(relationship["is_following"])

    def test_has_blocked_me_is_never_serialized(self):
        self._auth(self.a)
        data = self.client.get("/user/bravo/details").json()["data"]
        self.assertNotIn("has_blocked_me", data)
        self.assertNotIn("has_blocked_me", data["relationship"])

    def test_blocker_sees_no_owned_content(self):
        self._post(self.b)
        Highlight.objects.create(
            user=self.b,
            file_url="https://media.example.com/h.mp4",
            public_id="highlights/users/x/h.mp4",
            thumbnail_url="https://media.example.com/h.jpg",
        )
        self._auth(self.a)

        posts = self.client.get("/posts/list", {"username": "bravo"})
        self.assertEqual(posts.json()["data"]["count"], 0)

        highlights = self.client.get("/highlights/user/bravo")
        self.assertEqual(highlights.json()["data"]["count"], 0)

    def test_blocked_profile_404_is_byte_identical_to_unknown(self):
        """
        THE test. A body that differed by one word would tell a prober they
        were blocked rather than looking at a deleted account.
        """
        self._auth(self.b)

        blocked = self.client.get("/user/alpha/details")
        unknown = self.client.get("/user/nosuchpersonatall/details")

        self.assertEqual(blocked.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(blocked.status_code, unknown.status_code)
        self.assertEqual(
            json.dumps(blocked.json(), sort_keys=True),
            json.dumps(unknown.json(), sort_keys=True),
        )
        self.assertNotIn("block", json.dumps(blocked.json()).lower())

    def test_owned_content_endpoints_404_identically_for_the_blocked_party(self):
        self._auth(self.b)

        cases = (
            ("posts", "/posts/list?username=alpha",
             "/posts/list?username=nosuchpersonatall"),
            ("highlights", "/highlights/user/alpha",
             "/highlights/user/nosuchpersonatall"),
        )
        for label, blocked_url, unknown_url in cases:
            with self.subTest(surface=label):
                blocked = self.client.get(blocked_url)
                unknown = self.client.get(unknown_url)
                self.assertEqual(blocked.status_code, status.HTTP_404_NOT_FOUND)
                self.assertEqual(
                    json.dumps(blocked.json(), sort_keys=True),
                    json.dumps(unknown.json(), sort_keys=True),
                )

    def test_careers_and_achievements_404_for_the_blocked_party(self):
        self._auth(self.b)
        for label, url in (
            ("careers", f"/careers/users/{self.a.id}"),
            ("achievements", f"/achievements/users/{self.a.id}"),
        ):
            with self.subTest(surface=label):
                self.assertEqual(
                    self.client.get(url).status_code, status.HTTP_404_NOT_FOUND
                )

    def test_third_party_profile_is_unaffected(self):
        self._auth(self.c)
        resp = self.client.get("/user/bravo/details")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(resp.json()["data"]["is_blocked_by_me"])
        self.assertFalse(resp.json()["data"]["relationship"]["is_blocked"])

    def test_conversation_payloads_carry_is_blocked_and_keep_history(self):
        from messaging.models import ConversationParticipant

        convo = Conversation.objects.create(type=Conversation.Type.DIRECT)
        ConversationParticipant.objects.create(
            conversation=convo, user=self.a, has_accepted=True
        )
        ConversationParticipant.objects.create(
            conversation=convo, user=self.b, has_accepted=True
        )
        message = Message.objects.create(
            conversation=convo, sender_user=self.a, content="before the block"
        )
        convo.last_message = message
        convo.save(update_fields=["last_message"])

        for user in (self.a, self.b):
            with self.subTest(viewer=user.username):
                self._auth(user)

                detail = self.client.get(f"/conversations/{convo.id}/details")
                self.assertTrue(detail.json()["data"]["is_blocked"])

                listing = self.client.get("/conversations/list")
                payload = listing.json()["data"]
                rows = payload if isinstance(payload, list) else payload["results"]
                row = next(r for r in rows if r["id"] == str(convo.id))
                self.assertTrue(row["is_blocked"])

                # History stays readable — only the composer is disabled.
                messages = self.client.get(
                    "/conversations/messages/list",
                    {"conversation_id": str(convo.id)},
                )
                body = messages.json()["data"]
                rows = body if isinstance(body, list) else body["results"]
                self.assertEqual(len(rows), 1)


# =====================================================================
# ENDPOINTS
# =====================================================================

class BlockEndpointTests(BlockTestBase):

    def test_block_requires_authentication(self):
        self.client.force_authenticate(user=None)
        resp = self.client.post(
            BLOCK_URL,
            {"target_type": "user", "target_id": str(self.b.id)},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_blocked_list_requires_authentication(self):
        self.client.force_authenticate(user=None)
        self.assertEqual(
            self.client.get(BLOCKED_URL).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_post_creates_block(self):
        self._auth(self.a)
        resp = self.client.post(
            BLOCK_URL,
            {"target_type": "user", "target_id": str(self.b.id)},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.json()["data"]["is_blocked"])
        self.assertFalse(resp.json()["data"]["already_blocked"])

    def test_post_is_idempotent_over_http(self):
        self._auth(self.a)
        body = {"target_type": "user", "target_id": str(self.b.id)}
        self.client.post(BLOCK_URL, body, format="json")
        resp = self.client.post(BLOCK_URL, body, format="json")

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.json()["data"]["already_blocked"])
        self.assertEqual(Block.objects.count(), 1)

    def test_delete_unblocks(self):
        self._block(self.a_actor, target_user=self.b)
        self._auth(self.a)
        resp = self.client.delete(
            BLOCK_URL,
            {"target_type": "user", "target_id": str(self.b.id)},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(resp.json()["data"]["is_blocked"])
        self.assertEqual(Block.objects.count(), 0)

    def test_bad_target_type_is_400(self):
        self._auth(self.a)
        resp = self.client.post(
            BLOCK_URL,
            {"target_type": "banana", "target_id": str(self.b.id)},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(resp.json()["success"])

    def test_missing_target_id_is_400(self):
        self._auth(self.a)
        resp = self.client.post(BLOCK_URL, {"target_type": "user"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unknown_target_is_404(self):
        self._auth(self.a)
        resp = self.client.post(
            BLOCK_URL,
            {
                "target_type": "user",
                "target_id": "01a03cff-0000-7000-8000-000000000000",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_self_block_over_http_is_400(self):
        self._auth(self.a)
        resp = self.client.post(
            BLOCK_URL,
            {"target_type": "user", "target_id": str(self.a.id)},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.json()["message"], "Cannot block yourself")

    def test_org_actor_headers_are_respected(self):
        headers = self._auth(self.owner, org=self.org)
        resp = self.client.post(
            BLOCK_URL,
            {"target_type": "user", "target_id": str(self.b.id)},
            format="json",
            **headers,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(
            Block.objects.filter(blocker_org=self.org, blocked_user=self.b).exists()
        )

    def test_org_coach_gets_403_over_http(self):
        headers = self._auth(self.coach, org=self.org)
        resp = self.client.post(
            BLOCK_URL,
            {"target_type": "user", "target_id": str(self.b.id)},
            format="json",
            **headers,
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(Block.objects.count(), 0)

    def test_blocked_list_shows_only_blocks_i_made(self):
        self._block(self.a_actor, target_user=self.b)

        self._auth(self.a)
        body = self.client.get(BLOCKED_URL).json()["data"]
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["results"][0]["blocked"]["username"], "bravo")

        # The blocked party's own list stays empty — no disclosure.
        self._auth(self.b)
        self.assertEqual(self.client.get(BLOCKED_URL).json()["data"]["count"], 0)

    def test_blocked_list_pagination(self):
        targets = [self._user(f"target{i}") for i in range(5)]
        for target in targets:
            self._block(self.a_actor, target_user=target)

        self._auth(self.a)

        first = self.client.get(BLOCKED_URL, {"limit": 2, "offset": 0}).json()["data"]
        self.assertEqual(first["count"], 5)
        self.assertEqual(len(first["results"]), 2)
        self.assertTrue(first["has_more"])

        last = self.client.get(BLOCKED_URL, {"limit": 2, "offset": 4}).json()["data"]
        self.assertEqual(len(last["results"]), 1)
        self.assertFalse(last["has_more"])

    def test_blocked_list_clamps_a_silly_limit(self):
        self._auth(self.a)
        body = self.client.get(BLOCKED_URL, {"limit": 9999}).json()["data"]
        self.assertLessEqual(body["limit"], 50)

    def test_blocked_list_is_newest_first(self):
        first_target = self._user("first")
        second_target = self._user("second")
        self._block(self.a_actor, target_user=first_target)
        self._block(self.a_actor, target_user=second_target)

        self._auth(self.a)
        results = self.client.get(BLOCKED_URL).json()["data"]["results"]
        self.assertEqual(results[0]["blocked"]["username"], "second")


# =====================================================================
# WEBSOCKET
#
# Its own class, and a TRANSACTION test case: the consumer touches the DB from
# a worker thread, which cannot see rows held open inside APITestCase's
# per-test transaction. This one commits, so the socket sees the same database
# the test wrote.
# =====================================================================

@override_settings(
    CHANNEL_LAYERS=CHANNEL_LAYERS,
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class BlockWebSocketTests(APITransactionTestCase):

    def setUp(self):
        cache.clear()
        self.a = self._user("wsalpha")
        self.b = self._user("wsbravo")
        self.a_actor = Actor(actor_type="user", user=self.a)

        self.convo = Conversation.objects.create(type=Conversation.Type.DIRECT)
        ConversationParticipant.objects.create(
            conversation=self.convo, user=self.a, has_accepted=True
        )
        ConversationParticipant.objects.create(
            conversation=self.convo, user=self.b, has_accepted=True
        )

    def _user(self, username):
        user = User.objects.create_user(
            email=f"{username}@example.com", password="pass1234",
            username=username, role=User.Role.PLAYER,
        )
        accept_current_terms(user)
        UserProfile.objects.create(user=user, name=username.title())
        UsernameService.claim(username, user=user)
        return user

    async def _open(self, actor, user):
        communicator = WebsocketCommunicator(
            ChatConsumer.as_asgi(), f"/ws/chat/{self.convo.id}/"
        )
        communicator.scope["user"] = user
        communicator.scope["actor"] = actor
        communicator.scope["url_route"] = {
            "kwargs": {"conversation_id": str(self.convo.id)}
        }
        connected, _ = await communicator.connect()
        return communicator, connected

    def _send(self, text="hello?"):
        async def scenario():
            communicator, connected = await self._open(self.a_actor, self.a)
            if not connected:
                return {"__closed__": True}
            await communicator.send_to(text_data=json.dumps({"message": text}))
            event = json.loads(await communicator.receive_from(timeout=5))
            await communicator.disconnect()
            return event

        return async_to_sync(scenario)()

    def test_blocked_send_is_refused_without_closing_the_socket(self):
        BlockService.block(actor=self.a_actor, target_user=self.b)
        before = Message.objects.count()

        event = self._send()

        # A closed socket would itself tell the sender something happened.
        self.assertNotIn("__closed__", event)
        self.assertEqual(event["type"], "error")
        self.assertEqual(event["code"], "blocked")
        self.assertEqual(event["message"], BLOCKED_MESSAGE)
        self.assertEqual(Message.objects.count(), before)

    def test_normal_send_is_delivered(self):
        before = Message.objects.count()

        event = self._send("still fine")

        self.assertEqual(event["type"], "message")
        self.assertEqual(Message.objects.count(), before + 1)
