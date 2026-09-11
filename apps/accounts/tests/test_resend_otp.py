"""Re-sending the signup verification code.

Two things are worth pinning down here and they pull against each other.

The first is SILENCE. This endpoint takes an address and no proof of anything,
so an unknown address, a verified one and a genuine resend have to be one
answer — and the cooldown has to be part of that answer, or the second call
tells you what the first would not. That is the trap the tests below spend most
of their time on, because it is invisible: every one of those cases returns 200
and only the mail sender knows the difference.

The second is that the code actually goes somewhere it can be spent — under the
shared no-purpose OTP key, so /user/verify/otp keeps working on it.

The sender is patched where the VIEW looks it up, not where it is defined, and
the OTP is read out of the cache rather than off the captured call, so a code
that was mailed but never stored would still fail.

accounts.urls is mounted under /user/ (see core/urls.py).
"""

import time
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings

from rest_framework.test import APIClient

from apps.accounts.models import User, UserProfile
from apps.accounts.throttles import ResendOTPThrottle
from apps.accounts.views.user_auth_views import (
    RESEND_OTP_COOLDOWN_SECONDS,
    RESEND_OTP_GENERIC_MESSAGE,
)
from utils.cache_keys import CacheKeys

RESEND_URL = "/user/resend/otp"
VERIFY_URL = "/user/verify/otp"

PASSWORD = "password123"
UNVERIFIED_EMAIL = "pending@example.com"
VERIFIED_EMAIL = "done@example.com"
UNKNOWN_EMAIL = "nobody@example.com"

SENDER = "apps.accounts.views.user_auth_views.send_signup_otp_email"
WELCOME_SENDER = "apps.accounts.views.user_auth_views.send_welcome_email"


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)
class ResendSignupOTPTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        # LocMem is process-wide: wipe DRF's throttle history and any leftover
        # cooldown so the 3/min resend_otp bucket doesn't leak between tests.
        cache.clear()

        # Exactly what UserSignupAPIView leaves behind before the OTP is
        # verified — inactive AND unverified, which is the pair the view checks.
        self.pending = User.objects.create_user(
            email=UNVERIFIED_EMAIL,
            username="pendinguser",
            password=PASSWORD,
            role=User.Role.PLAYER,
            is_active=False,
        )
        UserProfile.objects.create(user=self.pending, name="Pending User")

        self.verified = User.objects.create_user(
            email=VERIFIED_EMAIL,
            username="verifieduser",
            password=PASSWORD,
            role=User.Role.PLAYER,
            is_email_verified=True,
        )
        UserProfile.objects.create(user=self.verified, name="Verified User")

    # ---------------- helpers ----------------

    def _resend(self, email=UNVERIFIED_EMAIL):
        """POST the endpoint with the mail sender patched out.

        Returns (response, sender_mock) — every test wants both, because what
        separates the cases here is never the response.
        """
        with patch(SENDER) as send_otp:
            res = self.client.post(RESEND_URL, {"email": email}, format="json")
        return res, send_otp

    def _clear_cooldown(self, email=UNVERIFIED_EMAIL):
        """Skip to the far side of the 30s wait without sleeping through it."""
        cache.delete(CacheKeys.otp_resend_cooldown(email.strip().lower()))

    def _reset_caller_throttle(self):
        """Forget the per-CALLER 3/min budget.

        Every request in this file comes from one test-client IP, so a case
        needing four calls would trip ResendOTPThrottle and get DRF's own 429 —
        a different body from the view's, and nothing to do with what is under
        test. The per-ADDRESS cooldown, which IS under test, is left alone.
        """
        cache.delete(
            ResendOTPThrottle.cache_format
            % {"scope": ResendOTPThrottle.scope, "ident": "127.0.0.1"}
        )

    def _issued_otp(self, email=UNVERIFIED_EMAIL):
        """The code sitting under the SHARED signup key, or None."""
        return cache.get(CacheKeys.email_otp(email))

    # ---------------- happy path ----------------

    def test_unverified_account_gets_a_fresh_code(self):
        res, send_otp = self._resend()

        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data["success"])
        self.assertEqual(res.data["message"], RESEND_OTP_GENERIC_MESSAGE)

        self.assertEqual(send_otp.call_count, 1)
        kwargs = send_otp.call_args.kwargs
        self.assertEqual(kwargs["email"], UNVERIFIED_EMAIL)
        self.assertEqual(kwargs["name"], "Pending User")

        # Stored, not just mailed — and under the key /user/verify/otp reads.
        self.assertEqual(self._issued_otp(), kwargs["otp"])

    def test_resent_code_verifies_the_account(self):
        """The point of the whole endpoint: the new code is spendable."""
        _, send_otp = self._resend()
        otp = send_otp.call_args.kwargs["otp"]

        with patch(WELCOME_SENDER):
            res = self.client.post(
                VERIFY_URL, {"email": UNVERIFIED_EMAIL, "otp": otp}, format="json"
            )

        self.assertEqual(res.status_code, 200, res.data)

        self.pending.refresh_from_db()
        self.assertTrue(self.pending.is_email_verified)
        self.assertTrue(self.pending.is_active)

    def test_a_resend_supersedes_the_previous_code(self):
        _, first = self._resend()
        self._clear_cooldown()
        _, second = self._resend()

        # One code per address at a time (the shared key is overwritten), and
        # it is the newest one that stands.
        self.assertEqual(self._issued_otp(), second.call_args.kwargs["otp"])

        res = self.client.post(
            VERIFY_URL,
            {"email": UNVERIFIED_EMAIL, "otp": first.call_args.kwargs["otp"]},
            format="json",
        )
        self.assertEqual(res.status_code, 400, res.data)

    # ---------------- silence ----------------

    def test_unknown_address_answers_exactly_as_a_real_one(self):
        real, real_send = self._resend()
        self._clear_cooldown()
        unknown, unknown_send = self._resend(UNKNOWN_EMAIL)

        self.assertEqual(unknown.status_code, real.status_code)
        self.assertEqual(unknown.data["success"], real.data["success"])
        self.assertEqual(unknown.data["message"], real.data["message"])

        # The ONLY difference is off the wire.
        self.assertEqual(real_send.call_count, 1)
        self.assertEqual(unknown_send.call_count, 0)

    def test_already_verified_account_answers_the_same_and_is_not_mailed(self):
        res, send_otp = self._resend(VERIFIED_EMAIL)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["message"], RESEND_OTP_GENERIC_MESSAGE)
        self.assertEqual(send_otp.call_count, 0)

        # Nothing was minted for an account that is already in.
        self.assertIsNone(self._issued_otp(VERIFIED_EMAIL))

    def test_the_cooldown_is_not_an_existence_oracle(self):
        """The trap: a cooldown set only on real sends would leak everything.

        Two calls for an unknown address must look like two calls for a real
        one. If the second answered 200 here and 429 there, the generic message
        on the first call would be worth nothing.
        """
        self._resend()
        real_second, _ = self._resend()

        self._reset_caller_throttle()

        self._resend(UNKNOWN_EMAIL)
        unknown_second, _ = self._resend(UNKNOWN_EMAIL)

        self.assertEqual(real_second.status_code, 429, real_second.data)
        self.assertEqual(unknown_second.status_code, 429, unknown_second.data)
        self.assertEqual(unknown_second.data["message"], real_second.data["message"])

    # ---------------- cooldown ----------------

    def test_second_send_inside_the_window_is_refused(self):
        self._resend()
        res, send_otp = self._resend()

        self.assertEqual(res.status_code, 429, res.data)
        self.assertFalse(res.data["success"])
        self.assertEqual(send_otp.call_count, 0)

        retry_after = res.data["data"]["retry_after"]
        self.assertGreater(retry_after, 0)
        self.assertLessEqual(retry_after, RESEND_OTP_COOLDOWN_SECONDS)

    def test_the_cooldown_is_per_address(self):
        """One inbox cooling down must not silence another."""
        self._resend()
        res, send_otp = self._resend(UNKNOWN_EMAIL)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(send_otp.call_count, 0)  # unknown address, correctly

        # ...and the real one is still the one holding a cooldown.
        blocked, _ = self._resend()
        self.assertEqual(blocked.status_code, 429, blocked.data)

    def test_case_variants_share_one_cooldown(self):
        self._resend()
        res, _ = self._resend(UNVERIFIED_EMAIL.upper())

        self.assertEqual(res.status_code, 429, res.data)

    def test_sending_again_is_allowed_once_the_window_passes(self):
        self._resend()

        # The stored value is the timestamp the cooldown lifts at; rewinding it
        # is how the far side of 30s is reached without a sleep in a test.
        key = CacheKeys.otp_resend_cooldown(UNVERIFIED_EMAIL)
        self.assertGreater(cache.get(key), time.time())
        cache.delete(key)

        res, send_otp = self._resend()
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(send_otp.call_count, 1)

    # ---------------- input ----------------

    def test_missing_email_is_refused(self):
        res = self.client.post(RESEND_URL, {}, format="json")

        self.assertEqual(res.status_code, 400, res.data)
        self.assertFalse(res.data["success"])

    def test_malformed_email_is_refused(self):
        """Not a leak: the shape of an address is knowable without asking us."""
        res = self.client.post(RESEND_URL, {"email": "not-an-email"}, format="json")

        self.assertEqual(res.status_code, 400, res.data)
        self.assertFalse(res.data["success"])

    def test_no_authentication_required(self):
        """Nobody has a token at this point in the signup."""
        res, _ = self._resend()

        self.assertNotEqual(res.status_code, 401)
        self.assertNotEqual(res.status_code, 403)
