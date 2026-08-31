"""
The consent gate, from the outside.

The subject of every test here is a user whose acceptance has gone stale — the
version bumped under them — because that is the state the gate exists for and
the state that is easy to get wrong in the direction that locks somebody out.

Staleness is produced by writing an old version onto the user rather than by
patching the registry, so the request goes through the real settings, the real
DEFAULT_PERMISSION_CLASSES and the real view classes. A test that patched
LEGAL_DOCUMENTS would prove the selector works and prove nothing about whether
the permission is actually WIRED to the endpoint it is supposed to guard.
"""

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User, UserProfile
from legal.constants import TERMS_VERSION
from legal.models import LegalAcceptance
from legal.permissions import EXEMPT_PATHS, TERMS_REQUIRED_CODE
from legal.selectors.acceptance_selectors import get_pending_documents
from legal.services.acceptance_service import record_acceptance
from usernames.services.username_service import UsernameService

STALE_VERSION = "2020-01-01"

ACCEPT_URL = "/legal/accept"
VERSIONS_URL = "/legal/versions"
DETAILS_URL = "/user/details"
FEED_URL = "/feed/list"
POSTS_URL = "/posts/create"


def make_user(email="stale@example.com", username="staleplayer"):
    user = User.objects.create_user(email=email, password="password123")
    UserProfile.objects.create(user=user, name="Stale Player")
    UsernameService.claim(username, user=user)
    return user


def make_stale(user):
    """
    A user who accepted, once, a version that is no longer current. The audit
    row stays — this is what a real version bump leaves behind.
    """
    record_acceptance(user=user, documents=["terms", "privacy"])
    LegalAcceptance.objects.filter(user=user, document="terms").update(
        version=STALE_VERSION
    )
    user.terms_version = STALE_VERSION
    user.save(update_fields=["terms_version", "updated_at"])
    user.refresh_from_db()
    return user


class GateTestCase(TestCase):

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = make_stale(make_user())
        self.client.force_authenticate(user=self.user)
        # Actor headers: every BaseAPIView resolves an actor before the view
        # body runs, and the gate must be what refuses the request, not a
        # missing header.
        self.client.credentials(
            HTTP_X_ACTOR_TYPE="user", HTTP_X_ACTOR_ID=str(self.user.id)
        )


class WritesAreBlockedTests(GateTestCase):

    def test_the_user_is_pending_to_begin_with(self):
        self.assertEqual(get_pending_documents(self.user), ["terms"])

    def test_creating_a_post_is_403(self):
        res = self.client.post(
            POSTS_URL, {"content": "hello"}, format="json"
        )

        self.assertEqual(res.status_code, 403, res.data)

    def test_the_403_body_is_machine_readable(self):
        # This is the contract the re-consent modal branches on. A generic
        # "detail" string would leave the client showing a toast that says
        # nothing and offers no way forward.
        res = self.client.post(
            POSTS_URL, {"content": "hello"}, format="json"
        )

        self.assertEqual(res.data["code"], TERMS_REQUIRED_CODE)
        self.assertEqual(res.data["pending_documents"], ["terms"])
        self.assertIn("detail", res.data)

    def test_an_accepted_user_can_write(self):
        # The other half of the same claim: the gate is what blocked the post
        # above, not something else about the request.
        record_acceptance(user=self.user, documents=["terms", "privacy"])
        self.user.refresh_from_db()

        res = self.client.post(
            POSTS_URL, {"content": "hello"}, format="json"
        )

        self.assertNotEqual(res.status_code, 403)

    def test_writes_outside_baseapiview_are_gated_too(self):
        # sports/user_sports_views.py is a plain APIView with its own
        # permission_classes, so it is only gated because the class was added
        # to it by hand. If that regressed, DEFAULT_PERMISSION_CLASSES would
        # NOT catch it — a view's own list replaces the setting.
        res = self.client.post(
            "/sports/user/sport/add", {"sport": "football"}, format="json"
        )

        self.assertEqual(res.status_code, 403, res.data)
        self.assertEqual(res.data["code"], TERMS_REQUIRED_CODE)


class ReadsAreAllowedTests(GateTestCase):

    def test_reading_the_feed_is_200(self):
        res = self.client.get(FEED_URL)

        self.assertEqual(res.status_code, 200, res.data)

    def test_reading_own_account_is_200(self):
        res = self.client.get(DETAILS_URL)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(
            res.data["data"]["legal"]["pending_documents"], ["terms"]
        )

    def test_reading_the_documents_is_200(self):
        res = self.client.get(VERSIONS_URL)

        self.assertEqual(res.status_code, 200, res.data)


class TheWayOutTests(GateTestCase):
    """The tests that matter most: a blocked user must be able to unblock."""

    def test_accepting_is_allowed_while_blocked(self):
        res = self.client.post(
            ACCEPT_URL, {"documents": ["terms"]}, format="json"
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["data"]["pending"], [])

    def test_accepting_actually_clears_the_gate(self):
        self.client.post(ACCEPT_URL, {"documents": ["terms"]}, format="json")

        res = self.client.post(
            POSTS_URL, {"content": "hello"}, format="json"
        )

        self.assertNotEqual(res.status_code, 403)
        self.user.refresh_from_db()
        self.assertEqual(self.user.terms_version, TERMS_VERSION)

    def test_the_old_acceptance_survives_the_new_one(self):
        self.client.post(ACCEPT_URL, {"documents": ["terms"]}, format="json")

        versions = set(
            LegalAcceptance.objects
            .filter(user=self.user, document="terms")
            .values_list("version", flat=True)
        )
        self.assertEqual(versions, {STALE_VERSION, TERMS_VERSION})

    def test_onboarding_can_still_be_finished(self):
        # A brand new account is gated the instant a version bumps mid-signup.
        # Being stuck between signup and a usable profile, unable to reach
        # either the gate or the exit, is the lockout this exemption prevents.
        res = self.client.post("/user/onboarding/complete", {}, format="json")

        self.assertEqual(res.status_code, 200, res.data)

    def test_logout_is_allowed_while_blocked(self):
        res = self.client.post("/user/logout", {}, format="json")

        self.assertNotEqual(res.status_code, 403)

    def test_every_exempt_path_is_a_real_route(self):
        """
        The exempt list is written by hand and matched by string. A typo in it
        does not fail loudly — it just silently gates an endpoint that was
        meant to stay open, which is the lockout bug in its quietest form.
        """
        from django.urls import Resolver404, resolve

        for path in EXEMPT_PATHS:
            try:
                resolve(path)
            except Resolver404:
                self.fail(f"EXEMPT_PATHS names a path that does not route: {path}")


class AnonymousAndSafeMethodTests(TestCase):

    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def test_an_anonymous_write_is_401_not_403(self):
        # The gate defers to IsAuthenticated for anonymous callers. Answering
        # first would tell a logged-out visitor to accept terms they have no
        # account to accept them with.
        res = self.client.post(POSTS_URL, {"content": "hello"}, format="json")

        self.assertEqual(res.status_code, 401)

    def test_the_google_login_url_stays_anonymous(self):
        # It was the one view relying on an unset DEFAULT_PERMISSION_CLASSES.
        # Now that the default is IsAuthenticated, a regression here means you
        # need a token to fetch the URL you log in with.
        res = self.client.get("/user/auth/google/login/url")

        self.assertNotIn(res.status_code, (401, 403))
