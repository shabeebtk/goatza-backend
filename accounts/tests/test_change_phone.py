"""Changing the phone number: the plain write, and the three things it guards.

Phone is not a credential, so there is no ceremony here — which makes the
non-obvious rule the one worth testing hardest: EVERY change resets
``is_phone_verified``. Nothing sets that flag True today, so the assertions
below are the only thing standing between now and an SMS-verification feature
that trusts a flag left over from a number the user no longer has.

accounts.urls is mounted under /user/ (see core/urls.py).
"""

from django.core.cache import cache
from django.test import TestCase, override_settings

from rest_framework.test import APIClient

from accounts.models import User, UserProfile
from accounts.throttles import PhoneChangeThrottle
from legal.testing import accept_current_terms

PHONE_URL = "/user/phone/change"

PASSWORD = "password123"
EMAIL = "phone@example.com"
OLD_PHONE = "+919876543210"
NEW_PHONE = "+919812345678"


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)
class ChangePhoneTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        # LocMem is process-wide: wipe DRF's throttle history so the 10/hour
        # phone_change bucket doesn't leak between tests.
        cache.clear()

        self.user = User.objects.create_user(
            email=EMAIL,
            phone=OLD_PHONE,
            username="phoneuser",
            password=PASSWORD,
            role=User.Role.PLAYER,
        )
        accept_current_terms(self.user)
        UserProfile.objects.create(user=self.user, name="Phone User")

        # Set by hand, because nothing in the app can: this is the stale
        # "verified" state the reset below exists to prevent.
        self.user.is_phone_verified = True
        self.user.save(update_fields=["is_phone_verified"])

        self.client.force_authenticate(user=self.user)

    def _post(self, phone):
        return self.client.post(PHONE_URL, {"phone": phone}, format="json")

    def _reload(self):
        self.user.refresh_from_db()
        return self.user

    # ---------------- happy path ----------------

    def test_valid_change_saves_and_clears_the_verified_flag(self):
        res = self._post(NEW_PHONE)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["data"]["phone"], NEW_PHONE)
        self.assertFalse(res.data["data"]["is_phone_verified"])

        user = self._reload()
        self.assertEqual(user.phone, NEW_PHONE)
        # THE POINT OF THIS ENDPOINT HAVING A SERVICE.
        self.assertFalse(user.is_phone_verified)

    def test_whitespace_is_stripped(self):
        res = self._post(f"  {NEW_PHONE}  ")

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self._reload().phone, NEW_PHONE)

    def test_plain_national_number_accepted(self):
        res = self._post("9876543211")

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self._reload().phone, "9876543211")

    def test_get_returns_the_current_number(self):
        """The settings screen's prefill — phone is in no user serializer."""
        res = self.client.get(PHONE_URL)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["data"]["phone"], OLD_PHONE)
        self.assertTrue(res.data["data"]["is_phone_verified"])

    # ---------------- removal ----------------

    def test_removing_the_phone_is_allowed_when_an_email_remains(self):
        res = self._post(None)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertIsNone(res.data["data"]["phone"])

        user = self._reload()
        # None, not "" — the column is unique, and two empty strings collide.
        self.assertIsNone(user.phone)
        self.assertFalse(user.is_phone_verified)

    def test_empty_string_also_means_remove(self):
        res = self._post("")

        self.assertEqual(res.status_code, 200, res.data)
        self.assertIsNone(self._reload().phone)

    def test_removing_the_only_identifier_is_refused(self):
        """
        user_email_or_phone_required is a CheckConstraint — a row with neither
        cannot exist, so this is a sentence rather than a 500.
        """
        phone_only = User.objects.create_user(
            phone="+919000000001",
            username="phoneonly",
            password=PASSWORD,
            role=User.Role.PLAYER,
        )
        accept_current_terms(phone_only)
        UserProfile.objects.create(user=phone_only, name="Phone Only")

        self.client.force_authenticate(user=phone_only)
        res = self._post(None)

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["data"]["code"], "phone_required")

        phone_only.refresh_from_db()
        self.assertEqual(phone_only.phone, "+919000000001")

    # ---------------- rejections ----------------

    def test_invalid_formats_rejected(self):
        # Too short, non-numeric, "+" in the wrong place, and one digit too
        # long to fit max_length=15 once the "+" is counted.
        for bad in ["1234567", "abcdefghij", "98765+43210", "+1234567890123456"]:
            with self.subTest(phone=bad):
                res = self._post(bad)

                self.assertEqual(res.status_code, 400, res.data)
                self.assertEqual(res.data["data"]["code"], "invalid_phone")

        # Untouched, and still verified — a rejected write changes nothing.
        user = self._reload()
        self.assertEqual(user.phone, OLD_PHONE)
        self.assertTrue(user.is_phone_verified)

    def test_duplicate_number_rejected(self):
        other = User.objects.create_user(
            email="other@example.com",
            phone=NEW_PHONE,
            username="otherphone",
            password=PASSWORD,
            role=User.Role.PLAYER,
        )
        UserProfile.objects.create(user=other, name="Other Phone")

        res = self._post(NEW_PHONE)

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["data"]["code"], "phone_taken")
        self.assertEqual(self._reload().phone, OLD_PHONE)

    def test_own_number_is_not_a_duplicate(self):
        """Re-saving what is already on file succeeds — it is the asked-for state."""
        res = self._post(OLD_PHONE)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self._reload().phone, OLD_PHONE)

    def test_unauthenticated_request_rejected(self):
        self.client.force_authenticate(user=None)

        res = self._post(NEW_PHONE)

        self.assertEqual(res.status_code, 401, res.data)
        self.assertEqual(self._reload().phone, OLD_PHONE)

    # ---------------- throttle ----------------

    def test_write_budget_runs_out(self):
        rate = PhoneChangeThrottle.THROTTLE_RATES["phone_change"]
        allowed = int(rate.split("/")[0])

        for _ in range(allowed):
            self.assertEqual(self._post(NEW_PHONE).status_code, 200)

        self.assertEqual(self._post(NEW_PHONE).status_code, 429)

        # The prefill read is NOT on that budget — see get_throttles.
        self.assertEqual(self.client.get(PHONE_URL).status_code, 200)
