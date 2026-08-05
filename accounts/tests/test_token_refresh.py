from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken,
    OutstandingToken,
)
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import User, UserProfile
from accounts.views.user_auth_views import GRACE_CACHE_KEY

# accounts.urls is mounted under /user/ (see core/urls.py)
REFRESH_URL = "/user/token/refresh"
LOGOUT_URL = "/user/logout"


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)
class TokenRefreshTests(TestCase):
    """Sliding 30-day session: every refresh rotates and restarts the clock."""

    def setUp(self):
        self.client = APIClient()
        # LocMem is process-wide: wipe both the grace entries and DRF's
        # throttle history so tests don't leak into each other.
        cache.clear()

        self.user = User.objects.create_user(
            email="refresh@example.com",
            username="refreshuser",
            password="password123",
            role=User.Role.PLAYER,
        )
        UserProfile.objects.create(user=self.user, name="Refresh User")

    def _issue_refresh(self, user=None):
        return RefreshToken.for_user(user or self.user)

    def _post_refresh(self, raw=None):
        if raw is not None:
            self.client.cookies["refresh_token"] = str(raw)
        return self.client.post(REFRESH_URL, {}, format="json")

    @staticmethod
    def _is_blacklisted(token):
        return BlacklistedToken.objects.filter(token__jti=token["jti"]).exists()

    # ---------------- missing cookie ----------------

    def test_missing_cookie_returns_401_refresh_missing(self):
        res = self.client.post(REFRESH_URL, {}, format="json")

        self.assertEqual(res.status_code, 401, res.data)
        self.assertFalse(res.data["success"])
        self.assertEqual(res.data["data"]["code"], "refresh_missing")

    # ---------------- happy path / rotation ----------------

    def test_refresh_rotates_and_sets_new_persistent_cookie(self):
        old = self._issue_refresh()

        res = self._post_refresh(old)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data["success"])
        self.assertTrue(res.data["data"]["access_token"])

        # A new refresh cookie was issued...
        self.assertIn("refresh_token", res.cookies)
        morsel = res.cookies["refresh_token"]
        self.assertNotEqual(morsel.value, str(old))
        self.assertTrue(morsel.value)

        # ...and it is persistent, not a session cookie (this was the bug).
        self.assertTrue(morsel["max-age"])
        self.assertEqual(int(morsel["max-age"]), 60 * 60 * 24 * 30)
        self.assertTrue(morsel["httponly"])
        self.assertTrue(morsel["secure"])

        # The rotated-away token is retired.
        self.assertTrue(self._is_blacklisted(old))

        # The new one is a usable, non-blacklisted refresh token.
        new = RefreshToken(morsel.value)
        self.assertEqual(str(new["user_id"]), str(self.user.id))
        self.assertFalse(self._is_blacklisted(new))

    # ---------------- 60s grace window ----------------

    def test_grace_replay_returns_same_pair(self):
        old = self._issue_refresh()

        first = self._post_refresh(old)
        self.assertEqual(first.status_code, 200, first.data)
        first_access = first.data["data"]["access_token"]
        first_refresh = first.cookies["refresh_token"].value

        # A second tab (or a retry after a lost Set-Cookie) replays the OLD
        # cookie — it must get the same pair back, not a logout.
        replay = self._post_refresh(old)

        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertTrue(replay.data["success"])
        self.assertEqual(replay.data["data"]["access_token"], first_access)
        self.assertEqual(replay.cookies["refresh_token"].value, first_refresh)

    def test_old_token_rejected_after_grace_expires(self):
        old = self._issue_refresh()
        old_jti = old["jti"]

        first = self._post_refresh(old)
        self.assertEqual(first.status_code, 200, first.data)

        # Simulate the 60s TTL lapsing.
        cache.delete(GRACE_CACHE_KEY.format(jti=old_jti))

        replay = self._post_refresh(old)

        self.assertEqual(replay.status_code, 401, replay.data)
        self.assertFalse(replay.data["success"])
        self.assertEqual(replay.data["data"]["code"], "refresh_invalid")

    # ---------------- dead sessions ----------------

    def test_inactive_user_gets_refresh_invalid(self):
        token = self._issue_refresh()
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])

        res = self._post_refresh(token)

        self.assertEqual(res.status_code, 401, res.data)
        self.assertFalse(res.data["success"])
        self.assertEqual(res.data["data"]["code"], "refresh_invalid")
        # No cookie handed back to a dead session.
        self.assertNotIn("refresh_token", res.cookies)

    def test_tampered_token_gets_refresh_invalid(self):
        res = self._post_refresh("not-a-jwt")

        self.assertEqual(res.status_code, 401, res.data)
        self.assertEqual(res.data["data"]["code"], "refresh_invalid")

    # ---------------- logout ----------------

    def test_logout_blacklists_and_deletes_cookie(self):
        token = self._issue_refresh()
        self.client.cookies["refresh_token"] = str(token)

        # No Authorization header on purpose: logout must work with an
        # already-expired access token.
        res = self.client.post(LOGOUT_URL, {}, format="json")

        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data["success"])

        # Cookie cleared.
        self.assertIn("refresh_token", res.cookies)
        morsel = res.cookies["refresh_token"]
        self.assertEqual(morsel.value, "")
        self.assertEqual(int(morsel["max-age"]), 0)

        self.assertTrue(self._is_blacklisted(token))

        # And the token is definitively dead for refresh.
        after = self._post_refresh(token)
        self.assertEqual(after.status_code, 401, after.data)
        self.assertEqual(after.data["data"]["code"], "refresh_invalid")

    def test_logout_without_cookie_still_succeeds(self):
        res = self.client.post(LOGOUT_URL, {}, format="json")

        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data["success"])
        self.assertEqual(res.cookies["refresh_token"].value, "")

    def test_logout_with_dead_token_still_succeeds(self):
        token = self._issue_refresh()
        token.blacklist()
        self.client.cookies["refresh_token"] = str(token)

        res = self.client.post(LOGOUT_URL, {}, format="json")

        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data["success"])
        self.assertEqual(res.cookies["refresh_token"].value, "")
        self.assertEqual(
            OutstandingToken.objects.filter(jti=token["jti"]).count(), 1
        )
