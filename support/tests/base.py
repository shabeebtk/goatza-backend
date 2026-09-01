"""
Shared fixtures for the support suite.

Not named ``test_*`` so the runner does not collect it as a module of tests —
same reason ``legal/testing.py`` is spelled the way it is.

``cache.clear()`` runs in every setUp because BOTH endpoints are throttled and
DRF keeps its counters in the shared cache. Without it a test's budget is
whatever the test before it left behind, and the failure surfaces as an
unexplained 429 in whichever test happens to run last.
"""

from django.conf import settings
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase

from accounts.models import User, UserProfile
from legal.testing import accept_current_terms
from organization.models import (
    Organization,
    OrganizationMember,
    OrganizationProfile,
)
from usernames.services.username_service import UsernameService

REPORT_URL = "/support/problem-report"
PUBLIC_REPORT_URL = "/public/support/problem-report"

# Long enough to clear MIN_DESCRIPTION_LENGTH without being a wall of text in
# every assertion.
DESCRIPTION = "The upload spinner never stops when I add a photo."


@override_settings(
    # Several users per test class; the real hasher makes setUp dominate the
    # run. Same override the moderation suite uses.
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class SupportTestBase(APITestCase):
    """
    One reporter, one bystander, and a club the reporter belongs to.

    The bystander exists for exactly one test — the screenshot key under
    somebody else's prefix — but it is the security case, so the fixture
    carries it rather than each test building a second account.
    """

    def setUp(self):
        cache.clear()

        self.user = self._user("reporter")
        self.other = self._user("bystander")

        self.org = self._org("dreamfc", "Dream FC")
        self.membership = OrganizationMember.objects.create(
            organization=self.org,
            user=self.user,
            role=OrganizationMember.Role.OWNER,
        )

    # ---------------- fixtures ----------------

    def _user(self, username, role=User.Role.PLAYER):
        user = User.objects.create_user(
            email=f"{username}@example.com",
            password="pass1234",
            username=username,
            role=role,
        )
        # Without this the consent gate answers 403 to every write — a fixture
        # user that skips it is not "a plain user", it is a pending one.
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

    # ---------------- requests ----------------

    def _auth(self, user=None, org=None):
        """
        Authenticate, and return the actor headers for an org caller.

        Returns a kwargs dict so call sites read
        ``self.client.post(URL, body, format="json", **self._auth(org=...))``.
        """
        self.client.force_authenticate(user=user or self.user)

        if org is None:
            return {}

        return {
            "HTTP_X_ACTOR_TYPE": "organization",
            "HTTP_X_ACTOR_ID": str(org.id),
        }

    def _body(self, **overrides):
        body = {"category": "media_upload", "description": DESCRIPTION}
        body.update(overrides)
        return body

    def _screenshot(self, user=None, org=None, name="shot.webp"):
        """
        A `{url, key}` pair the way the upload endpoint would have signed it:
        the key under the caller's own prefix, and the URL that key resolves to.
        """
        owner = (
            f"organizations/{org.id}" if org else f"users/{(user or self.user).id}"
        )
        key = f"{owner}/support/{name}"

        return {"url": f"{settings.MEDIA_PUBLIC_BASE_URL}/{key}", "key": key}
