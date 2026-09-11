"""Changing the login email: both proofs, and every way the flow refuses.

The account-level facts worth pinning down are all about ORDER — nothing may
be written before the new address has proved itself — and about SILENCE: a
wrong code, an expired one and no pending change at all must be one answer.

The OTP is read out of the cache rather than off a mocked sender, because what
is under test is the binding between the code and the address it was mailed
to; a captured argument would pass even if the two had come apart.

accounts.urls is mounted under /user/ (see core/urls.py).
"""

from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings

from rest_framework.test import APIClient

from apps.accounts.models import User, UserProfile
from apps.accounts.services.email_change_service import (
    EMAIL_CHANGE_OTP_PURPOSE,
    EMAIL_TAKEN_MESSAGE,
    INVALID_CODE_MESSAGE,
)
from apps.accounts.throttles import EmailChangeThrottle
from apps.legal.testing import accept_current_terms
from utils.cache_keys import CacheKeys

INITIATE_URL = "/user/email/change/initiate"
CONFIRM_URL = "/user/email/change/confirm"

PASSWORD = "password123"
OLD_EMAIL = "old@example.com"
NEW_EMAIL = "new@example.com"


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)
class ChangeEmailTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        # LocMem is process-wide: wipe DRF's throttle history so the 5/hour
        # email_change bucket doesn't leak between tests.
        cache.clear()

        self.user = User.objects.create_user(
            email=OLD_EMAIL,
            username="emailuser",
            password=PASSWORD,
            role=User.Role.PLAYER,
        )
        accept_current_terms(self.user)
        UserProfile.objects.create(user=self.user, name="Email User")

        self.client.force_authenticate(user=self.user)

    # ---------------- helpers ----------------

    def _initiate(self, new_email=NEW_EMAIL, password=PASSWORD):
        return self.client.post(
            INITIATE_URL,
            {"new_email": new_email, "password": password},
            format="json",
        )

    def _confirm(self, otp):
        return self.client.post(CONFIRM_URL, {"otp": otp}, format="json")

    def _issued_otp(self, email=NEW_EMAIL):
        """The code sitting under the purpose-scoped key, or None."""
        return cache.get(
            CacheKeys.email_otp(email, EMAIL_CHANGE_OTP_PURPOSE)
        )

    def _reload(self):
        self.user.refresh_from_db()
        return self.user

    # ---------------- happy path ----------------

    def test_initiate_then_confirm_moves_the_account(self):
        with patch(
            "apps.accounts.services.email_change_service.send_email_change_otp_email"
        ) as send_otp:
            res = self._initiate()

        self.assertEqual(res.status_code, 200, res.data)
        # Masked, never the address itself — this renders on a screen.
        self.assertEqual(res.data["data"]["sent_to"], "n*w@example.com")
        self.assertEqual(res.data["data"]["expires_in"], 600)

        # Mailed to the NEW address, not the one on the account.
        self.assertEqual(send_otp.call_args.kwargs["email"], NEW_EMAIL)

        # Nothing is written until the address has proved itself.
        self.assertEqual(self._reload().email, OLD_EMAIL)

        otp = self._issued_otp()
        self.assertIsNotNone(otp)

        with patch(
            "apps.accounts.services.email_change_service.send_email_changed_notice"
        ):
            res = self._confirm(otp)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["data"]["email"], NEW_EMAIL)

        user = self._reload()
        self.assertEqual(user.email, NEW_EMAIL)
        self.assertTrue(user.is_email_verified)

        # The binding is spent, so the flow cannot be re-run from it.
        self.assertIsNone(cache.get(CacheKeys.email_change_pending(user.id)))

    def test_confirm_notifies_the_old_address(self):
        with patch(
            "apps.accounts.services.email_change_service.send_email_change_otp_email"
        ):
            self._initiate()

        with patch(
            "apps.accounts.services.email_change_service.send_email_changed_notice"
        ) as notice:
            self._confirm(self._issued_otp())

        kwargs = notice.call_args.kwargs
        # The address being LEFT — the only inbox a victim still reads.
        self.assertEqual(kwargs["email"], OLD_EMAIL)
        # ...and the destination is masked, so this mail hands a reader nothing.
        self.assertEqual(kwargs["new_email"], "n*w@example.com")

    def test_new_email_is_normalized(self):
        with patch(
            "apps.accounts.services.email_change_service.send_email_change_otp_email"
        ):
            res = self._initiate(new_email="  Mixed@Example.COM  ")

        self.assertEqual(res.status_code, 200, res.data)

        with patch(
            "apps.accounts.services.email_change_service.send_email_changed_notice"
        ):
            self._confirm(self._issued_otp("mixed@example.com"))

        self.assertEqual(self._reload().email, "mixed@example.com")

    # ---------------- initiate rejections ----------------

    def test_wrong_password_rejected(self):
        with patch(
            "apps.accounts.services.email_change_service.send_email_change_otp_email"
        ) as send_otp:
            res = self._initiate(password="totally-wrong")

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["data"]["code"], "invalid_password")
        # No mail to an address the caller could not prove they own.
        send_otp.assert_not_called()
        self.assertIsNone(
            cache.get(CacheKeys.email_change_pending(self.user.id))
        )

    def test_google_only_account_told_to_set_a_password(self):
        """
        No usable password → the flow refuses and names the way out.

        Not "your password is incorrect": a Google-only account has never been
        shown a password, so that answer would be a dead end. See
        PASSWORD_NOT_SET_MESSAGE for why the password is required at all.
        """
        self.user.set_unusable_password()
        self.user.save(update_fields=["password"])

        with patch(
            "apps.accounts.services.email_change_service.send_email_change_otp_email"
        ) as send_otp:
            res = self._initiate(password="anything")

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["data"]["code"], "password_not_set")
        self.assertIn("Forgot password", res.data["message"])
        send_otp.assert_not_called()

    def test_taken_email_rejected(self):
        other = User.objects.create_user(
            email=NEW_EMAIL,
            username="takenuser",
            password=PASSWORD,
            role=User.Role.PLAYER,
        )
        UserProfile.objects.create(user=other, name="Taken User")

        with patch(
            "apps.accounts.services.email_change_service.send_email_change_otp_email"
        ) as send_otp:
            res = self._initiate()

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["data"]["code"], "email_taken")
        # Verbatim the signup flow's answer to the same collision.
        self.assertEqual(res.data["message"], EMAIL_TAKEN_MESSAGE)
        send_otp.assert_not_called()

    def test_invalid_email_rejected(self):
        res = self._initiate(new_email="not-an-email")

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["data"]["code"], "invalid_email")

    def test_own_email_rejected(self):
        res = self._initiate(new_email=OLD_EMAIL.upper())

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["data"]["code"], "same_email")

    def test_unauthenticated_request_rejected(self):
        self.client.force_authenticate(user=None)

        res = self._initiate()

        self.assertEqual(res.status_code, 401, res.data)
        self.assertEqual(self._reload().email, OLD_EMAIL)

    # ---------------- confirm rejections ----------------

    def test_wrong_code_rejected_with_the_generic_message(self):
        with patch(
            "apps.accounts.services.email_change_service.send_email_change_otp_email"
        ):
            self._initiate()

        res = self._confirm("0000")

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["data"]["code"], "invalid_code")
        self.assertEqual(res.data["message"], INVALID_CODE_MESSAGE)
        self.assertEqual(self._reload().email, OLD_EMAIL)

    def test_expired_code_is_indistinguishable_from_a_wrong_one(self):
        with patch(
            "apps.accounts.services.email_change_service.send_email_change_otp_email"
        ):
            self._initiate()

        otp = self._issued_otp()
        # What a 10-minute TTL looks like from here.
        cache.delete(CacheKeys.email_otp(NEW_EMAIL, EMAIL_CHANGE_OTP_PURPOSE))

        res = self._confirm(otp)

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["message"], INVALID_CODE_MESSAGE)

    def test_confirm_without_initiate_says_the_same_thing(self):
        """
        No pending change is NOT its own answer.

        Telling a caller "nothing pending" would confirm, to somebody holding a
        stolen access token, that nobody else's change is in flight — and the
        inverse leaks that one is.
        """
        res = self._confirm("1234")

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["data"]["code"], "invalid_code")
        self.assertEqual(res.data["message"], INVALID_CODE_MESSAGE)
        self.assertEqual(self._reload().email, OLD_EMAIL)

    def test_code_cannot_be_replayed(self):
        with patch(
            "apps.accounts.services.email_change_service.send_email_change_otp_email"
        ):
            self._initiate()

        otp = self._issued_otp()

        with patch(
            "apps.accounts.services.email_change_service.send_email_changed_notice"
        ):
            self.assertEqual(self._confirm(otp).status_code, 200)

        res = self._confirm(otp)

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["data"]["code"], "invalid_code")

    def test_email_claimed_between_the_steps_loses(self):
        """Ten minutes is long enough for somebody else to sign up with it."""
        with patch(
            "apps.accounts.services.email_change_service.send_email_change_otp_email"
        ):
            self._initiate()

        otp = self._issued_otp()

        other = User.objects.create_user(
            email=NEW_EMAIL,
            username="racer",
            password=PASSWORD,
            role=User.Role.PLAYER,
        )
        UserProfile.objects.create(user=other, name="Racer")

        res = self._confirm(otp)

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["data"]["code"], "email_taken")
        self.assertEqual(self._reload().email, OLD_EMAIL)

    # ---------------- throttle ----------------

    def test_both_endpoints_share_one_budget(self):
        """
        initiate and confirm are halves of one action, so they draw on the same
        allowance — a limit that let each run five times an hour would be a
        limit of ten on the thing that actually matters.

        Runs against the REAL configured rate rather than a patched one: the
        number in settings is the thing worth pinning down, because initiate
        mails a code to an address the caller typed.
        """
        rate = EmailChangeThrottle.THROTTLE_RATES["email_change"]
        allowed = int(rate.split("/")[0])

        with patch(
            "apps.accounts.services.email_change_service.send_email_change_otp_email"
        ):
            for _ in range(allowed):
                self.assertEqual(self._initiate().status_code, 200)

        # The budget is spent — and it is spent for the OTHER endpoint too.
        self.assertEqual(self._confirm("0000").status_code, 429)
