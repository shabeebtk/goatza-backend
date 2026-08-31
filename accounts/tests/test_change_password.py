from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import User, UserProfile
from legal.testing import accept_current_terms

# accounts.urls is mounted under /user/ (see core/urls.py)
CHANGE_PASSWORD_URL = "/user/change/password"
REFRESH_URL = "/user/token/refresh"

OLD_PASSWORD = "password123"
NEW_PASSWORD = "newpassword456"


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)
class ChangePasswordTests(TestCase):
    """Changing the password kills every OTHER session, keeps this one alive."""

    def setUp(self):
        self.client = APIClient()
        # LocMem is process-wide: wipe DRF's throttle history so the 5/hour
        # change_password bucket doesn't leak between tests.
        cache.clear()

        self.user = User.objects.create_user(
            email="change@example.com",
            username="changeuser",
            password=OLD_PASSWORD,
            role=User.Role.PLAYER,
        )
        accept_current_terms(self.user)
        UserProfile.objects.create(user=self.user, name="Change User")

        self.client.force_authenticate(user=self.user)

    def _post(self, current, new):
        return self.client.post(
            CHANGE_PASSWORD_URL,
            {"current_password": current, "new_password": new},
            format="json",
        )

    def _reload(self):
        self.user.refresh_from_db()
        return self.user

    # ---------------- rejections ----------------

    def test_wrong_current_password_rejected(self):
        res = self._post("totally-wrong", NEW_PASSWORD)

        self.assertEqual(res.status_code, 400, res.data)
        self.assertFalse(res.data["success"])
        self.assertEqual(res.data["data"]["code"], "invalid_current_password")

        # Password untouched.
        self.assertTrue(self._reload().check_password(OLD_PASSWORD))

    def test_weak_new_password_rejected(self):
        res = self._post(OLD_PASSWORD, "abc")  # < 6 chars, see utils.validations

        self.assertEqual(res.status_code, 400, res.data)
        self.assertFalse(res.data["success"])
        self.assertEqual(res.data["data"]["code"], "invalid_new_password")

        self.assertTrue(self._reload().check_password(OLD_PASSWORD))

    def test_same_password_rejected(self):
        res = self._post(OLD_PASSWORD, OLD_PASSWORD)

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["data"]["code"], "same_password")

    def test_missing_fields_rejected(self):
        res = self.client.post(CHANGE_PASSWORD_URL, {}, format="json")

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["data"]["code"], "missing_fields")

    def test_unauthenticated_request_rejected(self):
        self.client.force_authenticate(user=None)

        res = self._post(OLD_PASSWORD, NEW_PASSWORD)

        self.assertEqual(res.status_code, 401, res.data)
        self.assertTrue(self._reload().check_password(OLD_PASSWORD))

    # ---------------- happy path ----------------

    def test_success_rotates_session_and_swaps_password(self):
        res = self._post(OLD_PASSWORD, NEW_PASSWORD)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data["success"])
        self.assertTrue(res.data["data"]["access_token"])

        # A brand-new refresh cookie keeps THIS device logged in.
        self.assertIn("refresh_token", res.cookies)
        morsel = res.cookies["refresh_token"]
        self.assertTrue(morsel.value)
        self.assertTrue(morsel["httponly"])
        self.assertEqual(int(morsel["max-age"]), 60 * 60 * 24 * 30)

        # ...and that cookie still refreshes fine.
        self.client.cookies["refresh_token"] = morsel.value
        refreshed = self.client.post(REFRESH_URL, {}, format="json")
        self.assertEqual(refreshed.status_code, 200, refreshed.data)

        # Old password is dead, new one works.
        user = self._reload()
        self.assertFalse(user.check_password(OLD_PASSWORD))
        self.assertTrue(user.check_password(NEW_PASSWORD))

    def test_success_kills_other_devices(self):
        # A second device already signed in before the change.
        other_device = RefreshToken.for_user(self.user)

        res = self._post(OLD_PASSWORD, NEW_PASSWORD)
        self.assertEqual(res.status_code, 200, res.data)

        self.assertTrue(
            BlacklistedToken.objects.filter(token__jti=other_device["jti"]).exists()
        )

        # That device dies on its next refresh.
        other = APIClient()
        other.cookies["refresh_token"] = str(other_device)
        dead = other.post(REFRESH_URL, {}, format="json")

        self.assertEqual(dead.status_code, 401, dead.data)
        self.assertEqual(dead.data["data"]["code"], "refresh_invalid")
