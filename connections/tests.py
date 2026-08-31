from django.core.cache import cache
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User, UserProfile
from organization.models import (
    Organization,
    OrganizationProfile,
    OrganizationMember,
)
from connections.models import Follow
from usernames.services.username_service import UsernameService
from legal.testing import accept_current_terms

LIST_URL = "/connections/user/follow/list"


class FollowListAPITests(APITestCase):
    """
    FollowListAPIView — any authenticated actor may view any profile's
    followers / following / connections lists.
    """

    def setUp(self):
        cache.clear()  # username → profile lookups are cached

        self.alice = self._user("alice", "Alice Ant")
        self.bob = self._user("bob", "Bob Bee")
        self.carol = self._user("carol", "Carol Cat")
        self.dave = self._user("dave", "Dave Deer")

        self.org1 = self._org("dreamfc", "Dream FC")
        self.org2 = self._org("rivalfc", "Rival Club", verified=True)

        # alice acts through org1 in the org-actor tests.
        OrganizationMember.objects.create(
            organization=self.org1,
            user=self.alice,
            role=OrganizationMember.Role.OWNER,
        )

    # ── factories ────────────────────────────────────────────────

    def _user(self, username, name):
        user = User.objects.create_user(
            email=f"{username}@example.com",
            password="pass1234",
        )
        accept_current_terms(user)
        # Through the service, not straight onto the column: the handle only
        # resolves once UsernameRegistry holds it, and these tests fetch lists
        # BY username.
        UsernameService.claim(username, user=user)
        UserProfile.objects.create(
            user=user,
            name=name,
            headline=f"{name} headline",
            profile_photo=f"https://cdn.example.com/{username}.jpg",
        )
        return user

    def _org(self, username, name, verified=False):
        org = Organization.objects.create(
            name=name,
            username=username,
            type=Organization.Type.CLUB,
            is_verified=verified,
        )
        UsernameService.claim(username, organization=org)
        OrganizationProfile.objects.create(
            organization=org,
            logo=f"https://cdn.example.com/{username}.png",
            headline=f"{name} headline",
        )
        return org

    # ── request helpers ──────────────────────────────────────────

    def _list(self, actor_user, org=None, **params):
        self.client.force_authenticate(user=actor_user)
        headers = {}
        if org is not None:
            headers = {
                "HTTP_X_ACTOR_TYPE": "organization",
                "HTTP_X_ACTOR_ID": str(org.id),
            }
        return self.client.get(LIST_URL, params, **headers)

    def _rows(self, resp):
        return resp.data["data"]["results"]

    def _ids(self, resp):
        return [str(r["id"]) for r in self._rows(resp)]

    def _by_id(self, resp):
        return {str(r["id"]): r for r in self._rows(resp)}

    # ── 1. own lists (no username) — backward compatible ─────────

    def test_own_following_and_followers_default_to_actor(self):
        Follow.objects.create(follower_user=self.alice, following_user=self.bob)
        Follow.objects.create(follower_user=self.alice, following_org=self.org2)
        Follow.objects.create(follower_user=self.carol, following_user=self.alice)

        resp = self._list(self.alice, type="following")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(
            set(self._ids(resp)), {str(self.bob.id), str(self.org2.id)}
        )
        # actor follows everything in its own following list.
        self.assertTrue(all(r["is_following"] for r in self._rows(resp)))

        resp = self._list(self.alice, type="followers")
        self.assertEqual(self._ids(resp), [str(self.carol.id)])

    # ── 2. another user's lists via username ─────────────────────

    def test_other_users_lists_via_username(self):
        Follow.objects.create(follower_user=self.bob, following_user=self.carol)
        Follow.objects.create(follower_user=self.dave, following_user=self.bob)

        resp = self._list(self.alice, type="following", username="bob")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(self._ids(resp), [str(self.carol.id)])

        resp = self._list(self.alice, type="followers", username="bob")
        self.assertEqual(self._ids(resp), [str(self.dave.id)])

    # ── 3. org target via username ───────────────────────────────

    def test_org_target_lists_via_username(self):
        Follow.objects.create(follower_user=self.alice, following_org=self.org1)
        Follow.objects.create(follower_user=self.bob, following_org=self.org1)
        Follow.objects.create(follower_org=self.org1, following_user=self.carol)
        Follow.objects.create(follower_org=self.org1, following_org=self.org2)

        resp = self._list(
            self.dave, type="followers", username=self.org1.username
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(
            set(self._ids(resp)), {str(self.alice.id), str(self.bob.id)}
        )

        resp = self._list(
            self.dave, type="following", username=self.org1.username
        )
        self.assertEqual(
            set(self._ids(resp)), {str(self.carol.id), str(self.org2.id)}
        )
        org_row = self._by_id(resp)[str(self.org2.id)]
        self.assertEqual(org_row["type"], "organization")
        self.assertTrue(org_row["is_verified"])
        self.assertEqual(org_row["avatar"], self.org2.profile.logo)

    # ── 4. connections: mutual only, org target rejected ─────────

    def test_connections_include_mutual_exclude_one_way(self):
        # mutual alice <-> bob
        Follow.objects.create(follower_user=self.alice, following_user=self.bob)
        Follow.objects.create(follower_user=self.bob, following_user=self.alice)
        # one-way: alice -> carol (alice follows, carol doesn't follow back)
        Follow.objects.create(follower_user=self.alice, following_user=self.carol)
        # one-way: dave -> alice (dave follows alice, not mutual)
        Follow.objects.create(follower_user=self.dave, following_user=self.alice)

        # viewed by a third party via username
        resp = self._list(self.dave, type="connections", username="alice")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(self._ids(resp), [str(self.bob.id)])

        # and as the target's own default list
        resp = self._list(self.alice, type="connections")
        self.assertEqual(self._ids(resp), [str(self.bob.id)])

    def test_connections_rejected_for_org_target(self):
        resp = self._list(
            self.alice, type="connections", username=self.org1.username
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("user profiles", resp.data["message"])

        # org ACTOR default target is also an org → rejected
        resp = self._list(self.alice, org=self.org1, type="connections")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    # ── 5. search in each type ───────────────────────────────────

    def test_search_following(self):
        Follow.objects.create(follower_user=self.bob, following_user=self.carol)
        Follow.objects.create(follower_user=self.bob, following_user=self.dave)
        Follow.objects.create(follower_user=self.bob, following_org=self.org2)

        # profile-name match
        resp = self._list(
            self.alice, type="following", username="bob", search="Carol"
        )
        self.assertEqual(self._ids(resp), [str(self.carol.id)])

        # org-name match
        resp = self._list(
            self.alice, type="following", username="bob", search="Rival"
        )
        self.assertEqual(self._ids(resp), [str(self.org2.id)])

    def test_search_followers(self):
        Follow.objects.create(follower_user=self.carol, following_user=self.alice)
        Follow.objects.create(follower_user=self.dave, following_user=self.alice)

        resp = self._list(self.bob, type="followers", username="alice", search="dave")
        self.assertEqual(self._ids(resp), [str(self.dave.id)])

    def test_search_connections(self):
        Follow.objects.create(follower_user=self.alice, following_user=self.bob)
        Follow.objects.create(follower_user=self.bob, following_user=self.alice)
        Follow.objects.create(follower_user=self.alice, following_user=self.carol)
        Follow.objects.create(follower_user=self.carol, following_user=self.alice)

        resp = self._list(
            self.dave, type="connections", username="alice", search="bob"
        )
        self.assertEqual(self._ids(resp), [str(self.bob.id)])

    # ── 6. pagination ────────────────────────────────────────────

    def test_pagination_count_limit_offset(self):
        for u in (self.bob, self.carol, self.dave):
            Follow.objects.create(follower_user=self.alice, following_user=u)

        resp = self._list(self.alice, type="following", limit=2, offset=0)
        data = resp.data["data"]
        self.assertEqual(data["count"], 3)
        self.assertEqual(data["limit"], 2)
        self.assertEqual(data["offset"], 0)
        self.assertEqual(len(data["results"]), 2)

        resp = self._list(self.alice, type="following", limit=2, offset=2)
        self.assertEqual(resp.data["data"]["count"], 3)
        self.assertEqual(len(resp.data["data"]["results"]), 1)

        # limit is capped at 50, junk falls back to the default
        resp = self._list(self.alice, type="following", limit=999)
        self.assertEqual(resp.data["data"]["limit"], 50)
        resp = self._list(self.alice, type="following", limit="abc")
        self.assertEqual(resp.data["data"]["limit"], 20)

    # ── 7. is_following / is_me flags ────────────────────────────

    def test_flags_user_actor(self):
        # bob's followers: alice (self) and dave
        Follow.objects.create(follower_user=self.alice, following_user=self.bob)
        Follow.objects.create(follower_user=self.dave, following_user=self.bob)
        # the acting user (alice) follows dave
        Follow.objects.create(follower_user=self.alice, following_user=self.dave)

        resp = self._list(self.alice, type="followers", username="bob")
        rows = self._by_id(resp)

        alice_row = rows[str(self.alice.id)]
        self.assertTrue(alice_row["is_me"])
        self.assertFalse(alice_row["is_following"])

        dave_row = rows[str(self.dave.id)]
        self.assertFalse(dave_row["is_me"])
        self.assertTrue(dave_row["is_following"])

    def test_flags_org_actor(self):
        # bob's followers: org1 (the acting org) and alice
        Follow.objects.create(follower_org=self.org1, following_user=self.bob)
        Follow.objects.create(follower_user=self.alice, following_user=self.bob)
        # org1 follows alice
        Follow.objects.create(follower_org=self.org1, following_user=self.alice)

        resp = self._list(self.alice, org=self.org1, type="followers", username="bob")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rows = self._by_id(resp)

        org_row = rows[str(self.org1.id)]
        self.assertEqual(org_row["type"], "organization")
        self.assertTrue(org_row["is_me"])

        alice_row = rows[str(self.alice.id)]
        self.assertTrue(alice_row["is_following"])
        # acting as the org → the alice USER row is not "me"
        self.assertFalse(alice_row["is_me"])

    # ── 8. row shape + error cases ───────────────────────────────

    def test_unified_row_shape(self):
        Follow.objects.create(follower_user=self.alice, following_user=self.bob)
        Follow.objects.create(follower_user=self.alice, following_org=self.org2)

        resp = self._list(self.alice, type="following")
        rows = self._by_id(resp)

        expected_keys = {
            "type", "id", "username", "name", "avatar",
            "headline", "is_verified", "is_following", "is_me",
        }

        user_row = rows[str(self.bob.id)]
        self.assertEqual(set(user_row.keys()), expected_keys)
        self.assertEqual(user_row["type"], "user")
        self.assertEqual(user_row["name"], self.bob.profile.name)
        self.assertEqual(user_row["avatar"], self.bob.profile.profile_photo)
        self.assertFalse(user_row["is_verified"])

        org_row = rows[str(self.org2.id)]
        self.assertEqual(set(org_row.keys()), expected_keys)
        self.assertEqual(org_row["type"], "organization")
        self.assertEqual(org_row["name"], self.org2.name)
        self.assertEqual(org_row["avatar"], self.org2.profile.logo)

    def test_unknown_username_returns_404(self):
        resp = self._list(self.alice, type="following", username="ghost")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_invalid_type_returns_400(self):
        resp = self._list(self.alice, type="bogus")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
