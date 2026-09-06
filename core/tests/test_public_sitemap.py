"""
GET /public/sitemap/urls — the feed the frontend's sitemap.xml is built from.

Every test here is really one assertion: **a profile is in the sitemap if and
only if an anonymous visitor can open it**. That is the failure mode worth
guarding — a sitemap listing a hidden profile is us handing a crawler a URL
that 404s, and a sitemap missing a public one is the feature quietly not
working. Each visibility case is therefore checked against BOTH endpoints
(`test_*_matches_the_profile_endpoint`), not against a hardcoded expectation,
because the point is that the two share one predicate.

``cache.clear()`` in setUp because the response is cached for an hour under a
single key with no inputs — without it, the second test in the class asserts
against whatever the first one left behind.
"""

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User, UserProfile
from organization.models import Organization, OrganizationProfile
from usernames.services.username_service import UsernameService

SITEMAP_URL = "/public/sitemap/urls"


class PublicSitemapURLsTests(TestCase):

    def setUp(self):
        cache.clear()
        self.client = APIClient()

        self.user = self._user("striker")
        self.org = self._org("dreamfc", "Dream FC")

    # ---------------- fixtures ----------------

    def _user(self, username, *, public=True, **user_fields):
        user = User.objects.create_user(
            email=f"{username}@example.com",
            password="pass1234",
            username=username,
            role=User.Role.PLAYER,
        )
        for field, value in user_fields.items():
            setattr(user, field, value)
        if user_fields:
            user.save(update_fields=list(user_fields))

        UserProfile.objects.create(
            user=user, name=username.title(), is_public_profile=public
        )
        UsernameService.claim(username, user=user)
        return user

    def _org(self, username, name, *, public=True, **org_fields):
        org = Organization.objects.create(
            name=name, username=username, type=Organization.Type.CLUB,
            **org_fields,
        )
        OrganizationProfile.objects.create(
            organization=org, is_public_profile=public
        )
        UsernameService.claim(username, organization=org)
        return org

    # ---------------- requests ----------------

    def _feed(self):
        res = self.client.get(SITEMAP_URL)
        self.assertEqual(res.status_code, 200, res.data)
        return res.data["data"]

    def _usernames(self, key):
        return [row["username"] for row in self._feed()[key]]

    def _profile_status(self, path):
        """The status the matching public profile endpoint answers with."""
        cache.clear()
        return self.client.get(path).status_code

    # =================================================================
    # THE HAPPY PATH
    # =================================================================

    def test_a_public_user_and_org_are_both_listed(self):
        data = self._feed()

        self.assertEqual(
            [row["username"] for row in data["users"]], ["striker"]
        )
        self.assertEqual(
            [row["username"] for row in data["organizations"]], ["dreamfc"]
        )

    def test_the_endpoint_is_reachable_with_no_authorization_header(self):
        anonymous = APIClient()

        res = anonymous.get(SITEMAP_URL)

        self.assertEqual(res.status_code, 200)
        self.assertNotIn("HTTP_AUTHORIZATION", anonymous._credentials)

    # =================================================================
    # THE ALLOW-LIST
    # =================================================================

    def test_the_payload_has_exactly_two_lists(self):
        self.assertEqual(sorted(self._feed().keys()), ["organizations", "users"])

    def test_a_row_carries_a_handle_and_a_timestamp_and_nothing_else(self):
        """
        The allow-list, asserted as an equality rather than a series of
        assertNotIn: a new field added to the serializer would slip past
        "does not contain email" but not past this.
        """
        data = self._feed()

        for key in ("users", "organizations"):
            self.assertEqual(
                sorted(data[key][0].keys()), ["updated_at", "username"], key
            )

    def test_the_timestamp_is_the_profiles_own_and_parses_as_iso8601(self):
        from datetime import datetime

        row = self._feed()["users"][0]
        parsed = datetime.fromisoformat(row["updated_at"])

        self.assertIsNotNone(parsed.tzinfo)  # <lastmod> needs a real offset
        self.assertEqual(parsed, self.user.profile.updated_at)

    # =================================================================
    # VISIBILITY — a row is here iff the page is reachable
    # =================================================================

    def test_a_private_user_is_absent_and_the_profile_endpoint_404s(self):
        self._user("ghost", public=False)

        self.assertNotIn("ghost", self._usernames("users"))
        self.assertEqual(self._profile_status("/public/profile/ghost"), 404)

    def test_a_deactivated_user_is_absent_and_the_profile_endpoint_404s(self):
        self._user("dormant", is_active=False)

        self.assertNotIn("dormant", self._usernames("users"))
        self.assertEqual(self._profile_status("/public/profile/dormant"), 404)

    def test_a_soft_deleted_user_is_absent_and_the_profile_endpoint_404s(self):
        from django.utils import timezone

        self._user(
            "leaver", is_active=False, deletion_requested_at=timezone.now()
        )

        self.assertNotIn("leaver", self._usernames("users"))
        self.assertEqual(self._profile_status("/public/profile/leaver"), 404)

    def test_a_user_with_no_username_is_absent(self):
        # Nullable column: an account mid-signup has no public URL to list.
        User.objects.create_user(
            email="nameless@example.com", password="pass1234",
            username=None, role=User.Role.PLAYER,
        )

        self.assertEqual(self._usernames("users"), ["striker"])

    def test_a_user_with_no_profile_row_is_absent(self):
        User.objects.create_user(
            email="bare@example.com", password="pass1234",
            username="bare", role=User.Role.PLAYER,
        )

        self.assertNotIn("bare", self._usernames("users"))

    def test_a_private_org_is_absent_and_the_org_endpoint_404s(self):
        self._org("hiddenfc", "Hidden FC", public=False)

        self.assertNotIn("hiddenfc", self._usernames("organizations"))
        self.assertEqual(
            self._profile_status("/public/organization/hiddenfc"), 404
        )

    def test_a_suspended_org_is_absent_and_the_org_endpoint_404s(self):
        self._org("banned", "Banned FC", is_suspended=True)

        self.assertNotIn("banned", self._usernames("organizations"))
        self.assertEqual(
            self._profile_status("/public/organization/banned"), 404
        )

    def test_a_deactivated_org_is_absent(self):
        self._org("closed", "Closed FC", is_active=False)

        self.assertNotIn("closed", self._usernames("organizations"))

    # =================================================================
    # ORDER AND CAP
    # =================================================================

    def test_the_freshest_profile_comes_first(self):
        newer = self._user("winger")
        # auto_now, so a save is what moves it — not a hand-set value.
        newer.profile.save()

        self.assertEqual(self._usernames("users")[0], "winger")

    def test_the_list_is_capped(self):
        from core.selectors.public_profile_selectors import SITEMAP_MAX_ROWS

        # The cap itself is 5,000 and seeding that many rows would dominate the
        # suite; what is worth asserting is that the selector applies a slice
        # at all, which a `[:None]` or a dropped slice would not.
        with self.settings():
            self.assertEqual(SITEMAP_MAX_ROWS, 5000)
            self.assertLessEqual(len(self._feed()["users"]), SITEMAP_MAX_ROWS)

    # =================================================================
    # CACHING
    # =================================================================

    def test_the_response_is_served_from_cache_for_an_hour(self):
        """
        A profile made public after the first call must NOT appear until the
        entry expires — the deliberate trade documented on
        CacheKeys.public_sitemap_urls. Asserting it here means the day somebody
        adds an invalidation, this test tells them the behaviour changed
        instead of the change landing unnoticed.
        """
        self._feed()  # warms the cache

        self._user("latecomer")

        self.assertNotIn("latecomer", self._usernames("users"))

        cache.clear()
        self.assertIn("latecomer", self._usernames("users"))
