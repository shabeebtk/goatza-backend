"""Welcome + password-changed: the timestamp, the templates, and the triggers.

The interesting part of both emails is WHEN they fire. A welcome that arrives
before verification is a leak; a "your password was changed" that arrives when
nothing changed teaches people to ignore the one that matters. So most of this
file is about the success path being the only path.

accounts.urls is mounted under /user/ (see core/urls.py).
"""

from datetime import datetime, timezone as dt_timezone
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from rest_framework.test import APIClient

from accounts.models import User, UserProfile
from legal.testing import accept_current_terms
from utils.otp_validation import generate_otp
from utils.transactional_emails import (
    format_ist_timestamp,
    send_password_changed_email,
    send_welcome_email,
)

VERIFY_OTP_URL = "/user/verify/otp"
RESET_PASSWORD_URL = "/user/reset/password"
CHANGE_PASSWORD_URL = "/user/change/password"

NAME = "Arjun"
EMAIL = "arjun@example.com"
PASSWORD = "password123"
NEW_PASSWORD = "newpassword456"

# 13:12 UTC is 18:42 in Kolkata — the timestamp the reference file was
# approved with.
SAMPLE_CHANGED_AT = datetime(2026, 9, 4, 13, 12, tzinfo=dt_timezone.utc)
SAMPLE_CHANGED_AT_IST = "4 Sep 2026 at 6:42 PM IST"

BASE_URL = "https://goatza.com"


class ISTTimestampTests(SimpleTestCase):
    """The one piece of formatting in the email layer worth owning."""

    def test_a_utc_datetime_renders_as_ist_prose(self):
        self.assertEqual(
            format_ist_timestamp(SAMPLE_CHANGED_AT), SAMPLE_CHANGED_AT_IST
        )

    def test_no_leading_zero_on_the_day_or_the_hour(self):
        # 03:35 UTC -> 09:05 IST, same day. Minutes DO keep their zero: "9:5"
        # is not a time.
        dt = datetime(2026, 9, 4, 3, 35, tzinfo=dt_timezone.utc)

        self.assertEqual(format_ist_timestamp(dt), "4 Sep 2026 at 9:05 AM IST")

    def test_midnight_and_noon_are_twelve_not_zero(self):
        # 18:30 UTC rolls over into the next IST day.
        midnight = datetime(2026, 9, 3, 18, 30, tzinfo=dt_timezone.utc)
        noon = datetime(2026, 9, 4, 6, 30, tzinfo=dt_timezone.utc)

        self.assertEqual(
            format_ist_timestamp(midnight), "4 Sep 2026 at 12:00 AM IST"
        )
        self.assertEqual(
            format_ist_timestamp(noon), "4 Sep 2026 at 12:00 PM IST"
        )

    def test_a_naive_datetime_is_read_as_utc(self):
        naive = SAMPLE_CHANGED_AT.replace(tzinfo=None)

        self.assertEqual(format_ist_timestamp(naive), SAMPLE_CHANGED_AT_IST)


@override_settings(FRONTEND_BASE_URL=BASE_URL)
class LifecycleEmailRenderingTests(TestCase):
    """What actually lands in the inbox."""

    def _sent(self, send, **kwargs):
        with patch("utils.transactional_emails.send_email_async") as mock_send:
            send(**kwargs)

        self.assertEqual(mock_send.call_count, 1)
        return mock_send.call_args.kwargs

    def test_welcome_renders_the_three_steps_and_the_profile_link(self):
        kwargs = self._sent(send_welcome_email, name=NAME, email=EMAIL)
        html = kwargs["html_message"]

        self.assertEqual(kwargs["subject"], "Welcome to Goatza, Arjun \U0001f410")
        self.assertIn("You&rsquo;re in, Arjun", html)
        self.assertIn("Complete your profile", html)
        self.assertIn("Explore recruitments", html)
        self.assertIn("Build your network", html)
        self.assertIn(f'href="{BASE_URL}/profile"', html)
        self.assertIn("you just created a Goatza account", html)
        self.assertNotIn("{{", html)
        self.assertNotIn("{%", html)

    def test_welcome_plain_text_mirrors_the_html(self):
        text = self._sent(send_welcome_email, name=NAME, email=EMAIL)["message"]

        self.assertIn("Complete your profile", text)
        self.assertIn("Explore recruitments", text)
        self.assertIn("Build your network", text)
        self.assertIn(f"{BASE_URL}/profile", text)

    def test_password_changed_renders_the_time_and_the_reset_link(self):
        kwargs = self._sent(
            send_password_changed_email,
            name=NAME,
            email=EMAIL,
            changed_at=SAMPLE_CHANGED_AT,
        )
        html = kwargs["html_message"]

        self.assertEqual(kwargs["subject"], "Your Goatza password was changed")
        self.assertIn(SAMPLE_CHANGED_AT_IST, html)
        self.assertIn(f'href="{BASE_URL}/auth/forgot-password"', html)
        self.assertIn("If this was you, no action is needed.", html)
        self.assertIn("a security change on your Goatza account", html)
        self.assertNotIn("{{", html)
        self.assertNotIn("{%", html)

    def test_password_changed_plain_text_mirrors_the_html(self):
        text = self._sent(
            send_password_changed_email,
            name=NAME,
            email=EMAIL,
            changed_at=SAMPLE_CHANGED_AT,
        )["message"]

        self.assertIn(SAMPLE_CHANGED_AT_IST, text)
        self.assertIn(f"{BASE_URL}/auth/forgot-password", text)

    def test_changed_at_defaults_to_now(self):
        html = self._sent(
            send_password_changed_email, name=NAME, email=EMAIL
        )["html_message"]

        self.assertIn("IST", html)
        self.assertNotIn("{{", html)

    def test_the_recipient_name_is_escaped(self):
        html = self._sent(
            send_welcome_email, name="<script>alert(1)</script>", email=EMAIL
        )["html_message"]

        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)
class WelcomeEmailTriggerTests(TestCase):
    """Only a successful signup verification earns a welcome."""

    def setUp(self):
        # OTPThrottle counts in the shared cache, and generate_otp writes to
        # it too — start every test from a known-empty one.
        cache.clear()
        self.client = APIClient()

        self.user = User.objects.create_user(
            email=EMAIL,
            username="arjun",
            password=PASSWORD,
            role=User.Role.PLAYER,
            is_active=False,
        )
        accept_current_terms(self.user)
        UserProfile.objects.create(user=self.user, name=NAME)

    @patch("accounts.views.user_auth_views.send_welcome_email")
    def test_a_successful_verification_sends_exactly_one_welcome(self, mock_send):
        otp = generate_otp(EMAIL)

        res = self.client.post(
            VERIFY_OTP_URL, {"email": EMAIL, "otp": otp}, format="json"
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data["success"])

        self.assertEqual(mock_send.call_count, 1)
        self.assertEqual(
            mock_send.call_args.kwargs, {"name": NAME, "email": EMAIL}
        )

        # The mail announces a state that must already be true.
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_email_verified)
        self.assertTrue(self.user.is_active)

    @patch("accounts.views.user_auth_views.send_welcome_email")
    def test_a_wrong_otp_sends_nothing(self, mock_send):
        generate_otp(EMAIL)

        res = self.client.post(
            VERIFY_OTP_URL, {"email": EMAIL, "otp": "0000"}, format="json"
        )

        self.assertEqual(res.status_code, 400)
        mock_send.assert_not_called()

        self.user.refresh_from_db()
        self.assertFalse(self.user.is_email_verified)

    @patch("accounts.views.user_auth_views.send_welcome_email")
    def test_an_unknown_email_sends_nothing(self, mock_send):
        res = self.client.post(
            VERIFY_OTP_URL,
            {"email": "nobody@example.com", "otp": "1234"},
            format="json",
        )

        self.assertEqual(res.status_code, 404)
        mock_send.assert_not_called()

    @patch(
        "accounts.views.user_auth_views.send_welcome_email",
        side_effect=RuntimeError("template blew up"),
    )
    def test_a_raising_sender_does_not_cost_the_user_their_session(self, _mock):
        otp = generate_otp(EMAIL)

        res = self.client.post(
            VERIFY_OTP_URL, {"email": EMAIL, "otp": otp}, format="json"
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertIn("access", res.data["data"])


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)
class PasswordChangedEmailTriggerTests(TestCase):
    """Both password-writing paths notify; neither notifies on a rejection."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()

        self.user = User.objects.create_user(
            email=EMAIL,
            username="arjun",
            password=PASSWORD,
            role=User.Role.PLAYER,
        )
        accept_current_terms(self.user)
        UserProfile.objects.create(user=self.user, name=NAME)

    # ---------------- forgot-password flow ----------------

    @patch("accounts.views.user_auth_views.send_password_changed_email")
    def test_reset_password_sends_exactly_one_notice(self, mock_send):
        otp = generate_otp(EMAIL)

        res = self.client.post(
            RESET_PASSWORD_URL,
            {"email": EMAIL, "otp": otp, "new_password": NEW_PASSWORD},
            format="json",
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(mock_send.call_count, 1)
        self.assertEqual(
            mock_send.call_args.kwargs, {"name": NAME, "email": EMAIL}
        )

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW_PASSWORD))

    @patch("accounts.views.user_auth_views.send_password_changed_email")
    def test_reset_password_with_a_bad_otp_sends_nothing(self, mock_send):
        generate_otp(EMAIL)

        res = self.client.post(
            RESET_PASSWORD_URL,
            {"email": EMAIL, "otp": "0000", "new_password": NEW_PASSWORD},
            format="json",
        )

        self.assertEqual(res.status_code, 400)
        mock_send.assert_not_called()

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))

    @patch("accounts.views.user_auth_views.send_password_changed_email")
    def test_reset_password_with_a_rejected_password_sends_nothing(self, mock_send):
        # A valid OTP but a password the validator refuses: nothing is written,
        # so there is nothing to announce.
        otp = generate_otp(EMAIL)

        res = self.client.post(
            RESET_PASSWORD_URL,
            {"email": EMAIL, "otp": otp, "new_password": "abc"},
            format="json",
        )

        self.assertEqual(res.status_code, 400)
        mock_send.assert_not_called()

    @patch(
        "accounts.views.user_auth_views.send_password_changed_email",
        side_effect=RuntimeError("template blew up"),
    )
    def test_reset_password_still_succeeds_when_the_sender_raises(self, _mock):
        otp = generate_otp(EMAIL)

        res = self.client.post(
            RESET_PASSWORD_URL,
            {"email": EMAIL, "otp": otp, "new_password": NEW_PASSWORD},
            format="json",
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data["success"])

    # ---------------- logged-in change ----------------

    @patch("accounts.views.user_auth_views.send_password_changed_email")
    def test_change_password_sends_exactly_one_notice(self, mock_send):
        self.client.force_authenticate(user=self.user)

        res = self.client.post(
            CHANGE_PASSWORD_URL,
            {"current_password": PASSWORD, "new_password": NEW_PASSWORD},
            format="json",
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(mock_send.call_count, 1)
        self.assertEqual(
            mock_send.call_args.kwargs, {"name": NAME, "email": EMAIL}
        )

    @patch("accounts.views.user_auth_views.send_password_changed_email")
    def test_change_password_with_a_wrong_current_password_sends_nothing(
        self, mock_send
    ):
        self.client.force_authenticate(user=self.user)

        res = self.client.post(
            CHANGE_PASSWORD_URL,
            {"current_password": "totally-wrong", "new_password": NEW_PASSWORD},
            format="json",
        )

        self.assertEqual(res.status_code, 400)
        mock_send.assert_not_called()

    @patch(
        "accounts.views.user_auth_views.send_password_changed_email",
        side_effect=RuntimeError("template blew up"),
    )
    def test_change_password_still_succeeds_when_the_sender_raises(self, _mock):
        self.client.force_authenticate(user=self.user)

        res = self.client.post(
            CHANGE_PASSWORD_URL,
            {"current_password": PASSWORD, "new_password": NEW_PASSWORD},
            format="json",
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertIn("access_token", res.data["data"])
