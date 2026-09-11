"""
The guardian gate, from the outside.

The subject of every test here is a minor whose parent has not answered — the
state the lock exists for, and the state that is easy to get wrong in the
direction that strands a 15-year-old with an account nobody can unlock.

The lock is produced by signing a real minor up through the service, not by
writing a status onto a row, so every request travels the real settings, the
real DEFAULT_PERMISSION_CLASSES and the real view classes. A test that patched
the permission would prove the class works and prove nothing about whether it
is WIRED to the endpoints it is supposed to guard — which is the half that
actually breaks, because a view's own permission_classes silently replaces the
setting.
"""

import datetime

from django.core.cache import cache
from django.test import TestCase
from django.urls import Resolver404, resolve
from rest_framework.test import APIClient

from apps.accounts.models import User, UserProfile
from apps.guardians.permissions import (
    EXEMPT_PATHS,
    GUARDIAN_CONSENT_REQUIRED_CODE,
    HasGuardianConsentIfMinor,
)
from apps.guardians.services.consent_service import ensure_pending_for_minor
from apps.guardians.testing import grant_guardian_consent
from apps.legal.permissions import HasAcceptedCurrentTerms
from apps.legal.testing import accept_current_terms
from apps.usernames.services.username_service import UsernameService

DETAILS_URL = "/user/details"
FEED_URL = "/feed/list"
POSTS_URL = "/posts/create"
LOGOUT_URL = "/user/logout"
REFRESH_URL = "/user/token/refresh"
GUARDIAN_DETAILS_URL = "/guardian/details"

MINOR_YEARS = 14
ADULT_YEARS = 30


def years_ago(years):
    return datetime.date.today() - datetime.timedelta(days=365 * years)


def make_user(email, username, age_years):
    """
    An account built the way signup builds one: profile and birthdate in place,
    terms accepted, and ``ensure_pending_for_minor`` run — which is what locks
    a minor and leaves an adult alone.
    """
    user = User.objects.create_user(
        email=email, password="password123", country_code="IN"
    )
    UserProfile.objects.create(
        user=user, name="Gate Test", birthdate=years_ago(age_years)
    )
    UsernameService.claim(username, user=user)
    accept_current_terms(user)

    ensure_pending_for_minor(user)
    user.refresh_from_db()
    return user


class GateTestCase(TestCase):

    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def authenticate(self, user):
        self.client.force_authenticate(user=user)
        # Actor headers: every BaseAPIView resolves an actor before the view
        # body runs, and the gate must be what refuses the request, not a
        # missing header.
        self.client.credentials(
            HTTP_X_ACTOR_TYPE="user", HTTP_X_ACTOR_ID=str(user.id)
        )
        return user


class PendingMinorIsLockedTests(GateTestCase):

    def setUp(self):
        super().setUp()
        self.minor = self.authenticate(
            make_user("minor@example.com", "gateminor", MINOR_YEARS)
        )

    def test_the_minor_is_pending_to_begin_with(self):
        self.assertTrue(self.minor.is_minor)
        self.assertEqual(
            self.minor.guardian_consent_status,
            User.GuardianConsentStatus.PENDING,
        )

    def test_creating_a_post_is_403(self):
        res = self.client.post(POSTS_URL, {"content": "hello"}, format="json")

        self.assertEqual(res.status_code, 403, res.data)

    def test_the_403_body_is_machine_readable(self):
        # The contract the "waiting for a parent" screen branches on. A generic
        # "detail" string would leave a locked child looking at a toast that
        # says nothing and offers no way forward.
        res = self.client.post(POSTS_URL, {"content": "hello"}, format="json")

        self.assertEqual(res.data["code"], GUARDIAN_CONSENT_REQUIRED_CODE)
        self.assertEqual(
            res.data["guardian_consent_status"],
            User.GuardianConsentStatus.PENDING,
        )
        self.assertIn("detail", res.data)

    def test_reads_are_blocked_too(self):
        # THE DIFFERENCE FROM THE TERMS GATE, and the reason this gate exists:
        # a feed served to an unconsented child is that child's data being
        # processed. If this ever starts returning 200, the lock has quietly
        # become "write-only" and the DPDP claim behind it is gone.
        res = self.client.get(FEED_URL)

        self.assertEqual(res.status_code, 403, res.data)
        self.assertEqual(res.data["code"], GUARDIAN_CONSENT_REQUIRED_CODE)

    def test_views_outside_baseapiview_are_gated_too(self):
        # sports/user_sports_views.py is a plain APIView with its own
        # permission_classes, so it is only gated because the class was added
        # to it by hand. If that regressed, DEFAULT_PERMISSION_CLASSES would
        # NOT catch it — a view's own list replaces the setting.
        res = self.client.post(
            "/sports/user/sport/add", {"sport": "football"}, format="json"
        )

        self.assertEqual(res.status_code, 403, res.data)
        self.assertEqual(res.data["code"], GUARDIAN_CONSENT_REQUIRED_CODE)

    def test_a_withdrawn_minor_is_locked_just_as_hard(self):
        # A parent who took permission back asked for the processing to STOP.
        # A lock that let withdrawn accounts read would be ignoring them.
        self.minor.guardian_consent_status = User.GuardianConsentStatus.WITHDRAWN
        self.minor.save(update_fields=["guardian_consent_status"])

        res = self.client.get(FEED_URL)

        self.assertEqual(res.status_code, 403, res.data)
        self.assertEqual(
            res.data["guardian_consent_status"],
            User.GuardianConsentStatus.WITHDRAWN,
        )


class TheWayOutTests(GateTestCase):
    """The tests that matter most: a locked child must be able to unlock."""

    def setUp(self):
        super().setUp()
        self.minor = self.authenticate(
            make_user("wayout@example.com", "gatewayout", MINOR_YEARS)
        )

    def test_own_account_details_are_readable(self):
        # This response carries the `guardian` block the waiting screen renders
        # from. Gate it and the client cannot tell "locked" from "broken".
        res = self.client.get(DETAILS_URL)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(
            res.data["data"]["guardian"]["status"],
            User.GuardianConsentStatus.PENDING,
        )

    def test_naming_a_guardian_is_allowed_while_locked(self):
        res = self.client.post(
            GUARDIAN_DETAILS_URL,
            {"parent_name": "Priya Nair", "parent_email": "parent@example.com"},
            format="json",
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["data"]["mode"], "link_sent")

    def test_logout_is_allowed_while_locked(self):
        res = self.client.post(LOGOUT_URL, {}, format="json")

        self.assertNotEqual(res.status_code, 403)

    def test_token_refresh_is_allowed_while_locked(self):
        # Called with no cookie, so it fails on its own terms — a 401/400 is
        # fine and a 403 is not. The claim is that the GATE let it through.
        res = self.client.post(REFRESH_URL, {}, format="json")

        self.assertNotEqual(res.status_code, 403)

    def test_approval_actually_clears_the_gate(self):
        grant_guardian_consent(self.minor)

        res = self.client.get(FEED_URL)

        self.assertEqual(res.status_code, 200, res.data)
        self.minor.refresh_from_db()
        self.assertEqual(
            self.minor.guardian_consent_status,
            User.GuardianConsentStatus.APPROVED,
        )

    def test_every_exempt_path_is_a_real_route(self):
        """
        The exempt list is written by hand and matched by string. A typo in it
        does not fail loudly — it just silently locks an endpoint that was
        meant to stay open, and here that means a child who can never ask a
        parent and a support team that cannot fix it from their side.
        """
        for path in EXEMPT_PATHS:
            try:
                resolve(path)
            except Resolver404:
                self.fail(f"EXEMPT_PATHS names a path that does not route: {path}")


class ApprovedAndAdultAccountsTests(GateTestCase):

    def test_an_adult_is_untouched(self):
        adult = self.authenticate(
            make_user("adult@example.com", "gateadult", ADULT_YEARS)
        )

        self.assertFalse(adult.is_minor)
        self.assertEqual(
            adult.guardian_consent_status,
            User.GuardianConsentStatus.NOT_NEEDED,
        )

        self.assertEqual(self.client.get(FEED_URL).status_code, 200)
        self.assertNotEqual(
            self.client.post(
                POSTS_URL, {"content": "hello"}, format="json"
            ).status_code,
            403,
        )

    def test_an_approved_minor_passes(self):
        minor = make_user("approved@example.com", "gateapproved", MINOR_YEARS)
        grant_guardian_consent(minor)
        self.authenticate(minor)

        # Still a minor — every other minor protection stays on. The lock is
        # the only thing consent lifts.
        self.assertTrue(minor.is_minor)

        self.assertEqual(self.client.get(FEED_URL).status_code, 200)
        self.assertNotEqual(
            self.client.post(
                POSTS_URL, {"content": "hello"}, format="json"
            ).status_code,
            403,
        )

    def test_an_anonymous_request_is_401_not_403(self):
        # The gate defers to IsAuthenticated for anonymous callers. Answering
        # first would tell a logged-out visitor that somebody's parent has to
        # approve an account they do not have.
        res = self.client.post(POSTS_URL, {"content": "hello"}, format="json")

        self.assertEqual(res.status_code, 401)


class GateIsWiredEverywhereTests(TestCase):
    """
    The coverage claim, checked against the URLconf rather than trusted.

    A view's ``permission_classes`` REPLACES DEFAULT_PERMISSION_CLASSES, so the
    lock is only platform-wide for as long as every hand-rolled list carries
    it. The two gates are maintained as a pair for exactly this reason, and
    this test is what makes the pairing enforceable instead of a convention
    somebody remembers.
    """

    @staticmethod
    def _view_classes():
        from django.urls import get_resolver

        seen = {}

        def walk(patterns, prefix=""):
            for pattern in patterns:
                if hasattr(pattern, "url_patterns"):
                    walk(pattern.url_patterns, prefix + str(pattern.pattern))
                    continue

                view = getattr(pattern.callback, "cls", None)
                if view is not None:
                    seen[f"{view.__module__}.{view.__name__}"] = view

        walk(get_resolver().url_patterns)
        return seen

    def test_every_terms_gated_view_is_guardian_gated_too(self):
        missing = [
            name
            for name, view in self._view_classes().items()
            if HasAcceptedCurrentTerms in getattr(view, "permission_classes", [])
            and HasGuardianConsentIfMinor
            not in getattr(view, "permission_classes", [])
        ]

        self.assertEqual(
            missing,
            [],
            "These views gate on terms but not on guardian consent, so a "
            "locked minor can reach them: " + ", ".join(missing),
        )
